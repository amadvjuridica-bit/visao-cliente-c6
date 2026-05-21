# =========================
# app.py — COMPLETO
# =========================

import os
import io
import json
import re
import hashlib
import smtplib
import subprocess
import unicodedata
import datetime as dt
from functools import lru_cache
from typing import Dict, Tuple, Optional, List
from email.message import EmailMessage

import numpy as np
import pandas as pd
import streamlit as st

APP_DIR = os.path.dirname(os.path.abspath(__file__))

# =========================================================
# ✅ FIRESTORE (NUVEM)
# =========================================================
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except Exception:
    firebase_admin = None
    credentials = None
    firestore = None

@st.cache_resource
def _get_fs_db():
    """
    Inicializa Firestore usando st.secrets["firebase"] (Streamlit Cloud).
    Cacheado para não reinicializar a cada rerun.
    """
    if firebase_admin is None or credentials is None or firestore is None:
        return None
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
    try:
        snap = db.collection("app_store").document(doc_id).get()
        if not snap.exists:
            return default
        data = snap.to_dict() or {}
    except Exception:
        return default
    if data.get("chunked"):
        try:
            total = int(data.get("chunks") or 0)
            parts = []
            chunks_ref = db.collection("app_store").document(doc_id).collection("chunks")
            for idx in range(total):
                chunk = chunks_ref.document(f"{idx:05d}").get()
                if not chunk.exists:
                    return default
                parts.append((chunk.to_dict() or {}).get("data", ""))
            return json.loads("".join(parts))
        except Exception:
            return default
    return data.get("payload", default)

def _fs_save_payload(doc_id: str, obj):
    db = _get_fs_db()
    if db is None:
        return False
    doc_ref = db.collection("app_store").document(doc_id)
    try:
        raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        raw = json.dumps(json.loads(json.dumps(obj, ensure_ascii=False, default=str)), ensure_ascii=False, separators=(",", ":"))
    try:
        clean_obj = json.loads(raw)
    except Exception:
        clean_obj = obj
    max_chars = 650_000
    try:
        if len(raw.encode("utf-8")) <= max_chars:
            # Não faz leitura prévia: no Streamlit Cloud a cota de Firestore pode
            # estourar, e metadados/cache não podem derrubar o app.
            doc_ref.set({"payload": clean_obj, "chunked": False, "chunks": 0, "updated_at": firestore.SERVER_TIMESTAMP}, merge=False)
            return True
        chunks = []
        current = []
        current_size = 0
        for ch in raw:
            ch_size = len(ch.encode("utf-8"))
            if current and current_size + ch_size > max_chars:
                chunks.append("".join(current))
                current = [ch]
                current_size = ch_size
            else:
                current.append(ch)
                current_size += ch_size
        if current:
            chunks.append("".join(current))
        doc_ref.set({"payload": None, "chunked": True, "chunks": len(chunks), "updated_at": firestore.SERVER_TIMESTAMP}, merge=False)
        chunks_ref = doc_ref.collection("chunks")
        for idx, part in enumerate(chunks):
            chunks_ref.document(f"{idx:05d}").set({"data": part})
        return True
    except Exception:
        return False

def _fs_delete_doc(doc_id: str):
    db = _get_fs_db()
    if db is None:
        return
    doc_ref = db.collection("app_store").document(doc_id)
    try:
        snap = doc_ref.get()
        chunks = int(((snap.to_dict() or {}) if snap.exists else {}).get("chunks") or 0)
        for idx in range(chunks):
            doc_ref.collection("chunks").document(f"{idx:05d}").delete()
    except Exception:
        pass
    doc_ref.delete()


def _cloud_fast_open() -> bool:
    # O online agora lê o pacote versionado direto do deploy; manter "fast open"
    # ocultava históricos/datas no painel publicado.
    return False


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

# Preserva todo histórico disponível no app, inclusive bases antigas de 2025.
HIST_START = dt.date(2025, 1, 1)

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
ACELERADORES_NOVA = [
    (4000, 1.50),
    (2000, 1.25),
    (1000, 1.10),
    (0, 1.00),
]

CARTILHA_NOVA_MESES = {"04/2026", "05/2026", "06/2026"}
# Na planilha Analítico Visão Cliente do banco, MULTIPLICADOR_NOVA_CARTILHA sai 1 nas linhas;
# só ligue True se quiser usar a escada ACELERADORES_NOVA no app.
NOVA_CARTILHA_USAR_ACELERADORES = False
CAMPANHA_2TRI_METAS = {
    "04/2026": {"abertura": 731, "qualificacao": 666, "ativacao_pay": 28},
    "05/2026": {"abertura": 731, "qualificacao": 674, "ativacao_pay": 28},
    "06/2026": {"abertura": 768, "qualificacao": 642, "ativacao_pay": 27},
    "TRI": {"abertura": 2230, "qualificacao": 1982, "ativacao_pay": 83, "perc_min": 0.283},
}
SUPERVISOR_C6_MESES = ["04/2026", "05/2026", "06/2026"]
SUPERVISOR_C6_METAS = {
    "contas_abertas": {"meta": 835, "premio": 400.0},
    "contas_qualificadas": {"faixas": [(1000, 1400.0), (900, 540.0), (800, 500.0), (700, 400.0)]},
    "instalacao_c6pay": {"meta": 95, "premio": 360.0},
    "c6pay_ativada": {"meta": 55, "premio": 700.0},
    "pix_cnpj": {"meta": 0.65, "premio": 400.0},
    "domicilio_qualificado": {"meta": 30, "premio": 600.0},
    "spending_qualificado": {"meta": 60, "premio": 600.0},
    "wallet": {"meta": 0.60, "premio": 300.0},
    "ativacao_cartao": {"meta": 0.20, "premio": 400.0},
    "nivel4": {"meta": 300, "premio": 400.0},
}

DATA_DIR = os.path.join(APP_DIR, "data_store")
os.makedirs(DATA_DIR, exist_ok=True)

HIST_OPEN_DAILY = os.path.join(DATA_DIR, "hist_aberturas_diario.json")        # dd/mm/aaaa -> aberturas
HIST_LEADS_DAILY = os.path.join(DATA_DIR, "hist_cadastros_diario.json")       # dd/mm/aaaa -> cadastradas
HIST_MONTH_LEVELS = os.path.join(DATA_DIR, "hist_mes_cnpj_nivel.json")        # mm/aaaa -> {cnpj: nivel_max_no_mes}
HIST_PAGO_POR_CNPJ = os.path.join(DATA_DIR, "pago_max_por_cnpj.json")         # cnpj -> max pago acumulado
HIST_OLD_PAID_REF = os.path.join(DATA_DIR, "cartilha_antiga_pago_ref.json")   # mm/aaaa -> {cnpj: ja pago banco}
HIST_RESUMO_MENSAL = os.path.join(DATA_DIR, "resumo_mensal.json")             # mm/aaaa -> resumo calculado
HIST_SNAPSHOT_MENSAL = os.path.join(DATA_DIR, "snapshot_mensal.json")         # mm/aaaa -> estado (saldo/pix/domicilio/qualificadas)
HIST_VISAO_MENSAL = os.path.join(DATA_DIR, "visao_mensal_curada.json")        # mm/aaaa -> snapshot curado por cnpj
HIST_NOVA_PAGO_POR_CNPJ = os.path.join(DATA_DIR, "novo_pago_max_por_cnpj.json")
HIST_NOVA_PAID_REF = os.path.join(DATA_DIR, "cartilha_nova_pago_ref.json")    # mm/aaaa -> {cnpj: ja pago banco}
HIST_NOVA_RESUMO_MENSAL = os.path.join(DATA_DIR, "novo_resumo_mensal.json")
HIST_SUPERVISOR_C6_DAILY = os.path.join(DATA_DIR, "supervisor_c6_daily.json")
SUPERVISOR_C6_EMAIL_CFG = os.path.join(DATA_DIR, "supervisor_c6_email_config.json")
SUPERVISOR_C6_MONTHLY_METAS_PATH = os.path.join(DATA_DIR, "supervisor_c6_monthly_metas.json")

SMTP_HOST = "mail.amcob.com.br"
SMTP_PORT = 465
SMTP_PORT_TLS = 587
SMTP_SENDER = "am@amcob.com.br"
SMTP_DEFAULT_TO = "amadvjuridica@gmail.com"
SMTP_DEFAULT_PASSWORD = "748596Ml*."

# ✅ histórico comparativo diário (por DATA_BASE)
HIST_COMPARE_DAILY = os.path.join(DATA_DIR, "hist_comparativo_diario.json")   # dd/mm/aaaa -> métricas do dia

# Leads diários (Status diário - coluna Q)
LEADS_STATUS_DAILY_PATH = os.path.join(DATA_DIR, "leads_status_daily_q.json")
LEADS_CONTROL_PATH = os.path.join(DATA_DIR, "leads_control.json")

# ✅ Campanhas Meta (persistência)
META_SUMMARY_PATH = os.path.join(DATA_DIR, "meta_c6_summary.json")
META_GROUPS_PATH = os.path.join(DATA_DIR, "meta_c6_groups.json")
META_FILE_CONTROL = os.path.join(DATA_DIR, "meta_file_control.json")
META_GROUPS_CONTROL = os.path.join(DATA_DIR, "meta_groups_control.json")
C6_DAILY_VISAO_CACHE = os.path.join(DATA_DIR, "c6_daily_visao_cache.xlsx")
C6_DAILY_LEADS_CACHE = os.path.join(DATA_DIR, "c6_daily_leads_cache.xlsx")
C6_DAILY_LCT_CACHE = os.path.join(DATA_DIR, "c6_daily_lct_cache.bin")
C6_DAILY_LCT_CLOUD_CACHE = os.path.join(DATA_DIR, "c6_daily_lct_cache_compacto.json")
C6_DAILY_IMPORT_META = os.path.join(DATA_DIR, "c6_daily_import_meta.json")
C6_DAILY_FUNIL_TRACK = os.path.join(DATA_DIR, "c6_daily_funil_track.json")
C6_LEADS_CNPJ_TRACK = os.path.join(DATA_DIR, "c6_leads_cnpj_track.json")
C6_OPS_CACHE = os.path.join(DATA_DIR, "c6_operacao_ops_cache.bin")
C6_OPS_CACHE_META = os.path.join(DATA_DIR, "c6_operacao_ops_cache_meta.json")
PANEL_C6_REFRESH_META = os.path.join(DATA_DIR, "panel_c6_refresh_meta.json")
PANEL_C6_INCREMENTAL_CACHE = os.path.join(DATA_DIR, "panel_c6_incremental_cache.json")
PANEL_C6_CARTILHA_NOVA_CACHE = os.path.join(DATA_DIR, "panel_c6_cartilha_nova_cache.json")
REMUN_ENGINE_VERSION = "2026-05-19-wallet-direta-v7"

PIX_VALID_VALUES = {
    "CNPJ",
    "EMAIL",
    "CNPJ|PHONE",
    "CNPJ|EMAIL",
    "PHONE",
    "CNPJ|EMAIL|PHONE",
    "EMAIL|PHONE",
}


# =========================================================
# HELPERS - EXATAMENTE COMO ESTAVAM
# =========================================================
_MISSING = object()


def _bundled_seed_payload(doc_id: str, default):
    """Lê o payload versionado no deploy sem copiar a base inteira para o Firestore."""
    seed_path = os.path.join(DATA_DIR, "cloud_seed_version.json")
    if not os.path.exists(seed_path):
        return default
    try:
        with open(seed_path, "r", encoding="utf-8") as f:
            seed = json.load(f)
    except Exception:
        return default
    source_name = ""
    doc_base = os.path.basename(str(doc_id or ""))
    for entry in seed.get("files", []):
        if isinstance(entry, dict):
            target = os.path.basename(str(entry.get("target") or entry.get("source") or ""))
            source = os.path.basename(str(entry.get("source") or ""))
        else:
            target = os.path.basename(str(entry or ""))
            source = target
        if target == doc_base:
            source_name = source
            break
    if not source_name:
        source_name = doc_base if doc_base.endswith(".json") else ""
    if not source_name or not source_name.endswith(".json"):
        return default
    path = os.path.join(DATA_DIR, source_name)
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def safe_json_load(path: str, default):
    """
    ✅ Se existir st.secrets["firebase"], lê do Firestore.
    Caso contrário, mantém comportamento local.
    """
    if "firebase" in st.secrets:
        doc_id = _fs_doc_id_from_path(path)
        session_payloads = st.session_state.get("_cloud_session_payloads", {})
        if isinstance(session_payloads, dict) and doc_id in session_payloads:
            return session_payloads.get(doc_id, default)
        cloud_payload = _fs_load_payload(doc_id, _MISSING)
        if cloud_payload is not _MISSING:
            return cloud_payload
        bundled = _bundled_seed_payload(doc_id, _MISSING)
        if bundled is not _MISSING:
            return bundled
        return default

    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            try:
                ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                os.replace(path, f"{path}.corrompido_{ts}.json")
            except Exception:
                pass
            return default
    return default


def safe_json_save(path: str, obj):
    """
    ✅ Se existir st.secrets["firebase"], salva no Firestore.
    Caso contrário, mantém comportamento local.
    """
    if "firebase" in st.secrets:
        doc_id = _fs_doc_id_from_path(path)
        st.session_state.setdefault("_cloud_session_payloads", {})[doc_id] = obj
        return _fs_save_payload(doc_id, obj)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    return True


def safe_json_delete(path: str):
    """
    Remove somente o doc/arquivo daquele relatório.
    """
    if "firebase" in st.secrets:
        doc_id = _fs_doc_id_from_path(path)
        st.session_state.setdefault("_cloud_session_payloads", {}).pop(doc_id, None)
        _fs_delete_doc(doc_id)
    if os.path.exists(path):
        os.remove(path)


def local_json_load(path: str, default):
    if "firebase" in st.secrets:
        return safe_json_load(path, default)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            try:
                ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                os.replace(path, f"{path}.corrompido_{ts}.json")
            except Exception:
                pass
            return default
    return default


def local_json_save(path: str, obj):
    if "firebase" in st.secrets:
        return safe_json_save(path, obj)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    return True


def local_json_delete(path: str):
    if "firebase" in st.secrets:
        safe_json_delete(path)
        return
    if os.path.exists(path):
        os.remove(path)


def _bootstrap_cloud_from_bundled_data():
    """Atualiza a base Firestore do app publicado com os JSONs versionados no deploy."""
    if "firebase" not in st.secrets:
        return
    seed_path = os.path.join(DATA_DIR, "cloud_seed_version.json")
    if not os.path.exists(seed_path):
        return
    try:
        with open(seed_path, "r", encoding="utf-8") as f:
            seed = json.load(f)
    except Exception:
        return
    version = str(seed.get("version") or "").strip()
    if not version:
        return
    if not seed.get("copy_to_firestore"):
        try:
            _fs_save_payload("cloud_seed_version.json", {
                "version": version,
                "generated_at": seed.get("generated_at", ""),
                "files_count": len(seed.get("files", []) or []),
                "mode": "bundled-fast-open",
            })
        except Exception:
            pass
        return
    current = _fs_load_payload("cloud_seed_version.json", default={}) or {}
    if str(current.get("version") or "") == version:
        return
    for entry in seed.get("files", []):
        if isinstance(entry, dict):
            source_name = os.path.basename(str(entry.get("source") or ""))
            target_name = os.path.basename(str(entry.get("target") or source_name))
        else:
            source_name = os.path.basename(str(entry or ""))
            target_name = source_name
        if not source_name.endswith(".json") or source_name.startswith("cloud_seed_version"):
            continue
        if not target_name:
            continue
        path = os.path.join(DATA_DIR, source_name)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            _fs_save_payload(target_name, payload)
        except Exception:
            continue
    _fs_save_payload("cloud_seed_version.json", seed)


def _df_to_store_payload(df: pd.DataFrame) -> dict:
    try:
        return json.loads(df.reset_index(drop=True).to_json(orient="split", date_format="iso", force_ascii=False))
    except Exception:
        safe_df = df.reset_index(drop=True).copy()
        for col in safe_df.columns:
            safe_df[col] = safe_df[col].astype("string")
        return json.loads(safe_df.to_json(orient="split", force_ascii=False))


def _df_from_store_payload(payload) -> Optional[pd.DataFrame]:
    if not isinstance(payload, dict):
        return None
    try:
        df = pd.read_json(io.StringIO(json.dumps(payload, ensure_ascii=False)), orient="split")
    except Exception:
        try:
            cols = payload.get("columns") or []
            data = payload.get("data") or []
            df = pd.DataFrame(data, columns=cols)
        except Exception:
            return None
    try:
        if not isinstance(df.index, pd.RangeIndex):
            first_col = str(df.columns[0]) if len(df.columns) else ""
            idx_s = pd.Series(df.index, index=df.index).astype("string").fillna("")
            col_s = df.iloc[:, 0].astype("string").fillna("") if len(df.columns) else pd.Series([], dtype="string")
            if first_col.lower().startswith("unnamed") or idx_s.str.strip().ne("").mean() > 0.8:
                if len(df.columns) == 0 or idx_s.ne(col_s).mean() > 0.8:
                    df.insert(0, "nome_cliente_index", idx_s.to_numpy())
            df = df.reset_index(drop=True)
    except Exception:
        try:
            df = df.reset_index(drop=True)
        except Exception:
            pass
    return df


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


def _month_key_to_date(mkey: str) -> Optional[dt.date]:
    try:
        mm, aa = str(mkey).split("/")
        return dt.date(int(aa), int(mm), 1)
    except Exception:
        return None


def _shift_month_key(mkey: str, delta_months: int) -> str:
    base = _month_key_to_date(mkey)
    if not base:
        return ""
    month_zero = (base.year * 12 + base.month - 1) + int(delta_months)
    year = month_zero // 12
    month = month_zero % 12 + 1
    return f"{month:02d}/{year}"


def _vigent_open_month_keys(base_mkey: str) -> List[str]:
    keys = []
    for delta in [0, -1, -2]:
        mk = _shift_month_key(base_mkey, delta)
        if mk:
            keys.append(mk)
    return keys


def _normalize_text_value(v) -> str:
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except Exception:
        pass
    return str(v).strip()


def _normalize_scalar_text(v) -> str:
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except Exception:
        pass
    return str(v).strip()


def _focus_phone(v) -> str:
    digits = re.sub(r"\D+", "", _normalize_text_value(v))
    if not digits:
        return ""
    if not digits.startswith("55"):
        digits = f"55{digits}"
    return digits


def _focus_phone_pair(row: dict) -> Tuple[str, str]:
    phones = []
    for raw in [row.get("telefone"), row.get("telefone_master")]:
        p = _focus_phone(raw)
        if p and p not in phones:
            phones.append(p)
    while len(phones) < 2:
        phones.append("")
    return phones[0], phones[1]


def to_date_series(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.date


def normalize_str(s: pd.Series) -> pd.Series:
    return s.astype("string").fillna("").str.strip()


def _normalize_person_key(value: str) -> str:
    if value is None or pd.isna(value):
        txt = ""
    else:
        txt = str(value).strip().upper()
    txt = unicodedata.normalize("NFKD", txt).encode("ASCII", "ignore").decode("ASCII")
    txt = re.sub(r"[_\-.]+", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


def _canonicalize_operator_series(series: pd.Series, remun_cfg: Optional[dict] = None) -> pd.Series:
    values = series.astype("string").fillna("").str.strip()
    cfg_ops = (remun_cfg or {}).get("operadores") if isinstance(remun_cfg, dict) else {}
    cfg_names = {}
    if isinstance(cfg_ops, dict):
        for name in cfg_ops.keys():
            key = _normalize_person_key(name)
            if key:
                cfg_names[key] = str(name).strip()

    preferred = {}
    for raw in values.tolist():
        key = _normalize_person_key(raw)
        if not key:
            continue
        if key in cfg_names:
            preferred[key] = cfg_names[key]
        elif key not in preferred:
            preferred[key] = str(raw).strip()

    return values.apply(lambda raw: preferred.get(_normalize_person_key(raw), str(raw).strip()))


def _extract_date_from_filename(name: str) -> Optional[dt.date]:
    txt = str(name or "")
    m = re.search(r"(\d{2})(\d{2})(\d{4})", txt)
    if not m:
        return None
    try:
        return dt.datetime.strptime("".join(m.groups()), "%d%m%Y").date()
    except Exception:
        return None


def _days_since_today_exclusive(start_ts, ref_date: Optional[dt.date] = None) -> Optional[int]:
    if pd.isna(start_ts):
        return None
    try:
        start_day = pd.Timestamp(start_ts).date()
        ref_day = ref_date or dt.date.today()
        diff = (ref_day - start_day).days
        if diff < 0:
            return None
        return int(diff)
    except Exception:
        return None


def _is_blocked_followup_status(status: str) -> bool:
    txt = _normalize_person_key(status)
    blocked_terms = [
        "CONTA ABERTA",
        "DESACORDO COMERCIAL",
        "AGUARDANDO ATUACAO MANUAL BKO",
        "AGUARDAR ATUACAO MANUAL BKO",
    ]
    return any(term in txt for term in blocked_terms)


def _is_actionable_followup_status(status: str) -> bool:
    txt = _normalize_person_key(status)
    if not txt or _is_blocked_followup_status(txt):
        return False
    actionable_terms = [
        "AINDA NAO INICIOU A ABERTURA",
        "AINDA NAO INICIOU ABERTURA",
        "DEVE ESTAR FAZENDO UM NOVO PROCESSO",
        "NOVO PROCESSO",
        "REFAZER",
        "ORIENTAR CLIENTE",
        "ORIENTAR O CLIENTE",
        "ORIENTAR A ENVIAR",
        "ENVIAR DOCUMENTO",
        "ENVIAR DOCUMENTOS",
        "DOCUMENTO",
        "DOCUMENTACAO",
    ]
    return any(term in txt for term in actionable_terms)


def _read_previous_message_file(name: str, raw_bytes: bytes) -> Optional[pd.DataFrame]:
    try:
        lower = str(name or "").lower()
        if lower.endswith(".csv"):
            sample = raw_bytes[:200_000].decode("utf-8-sig", errors="replace")
            sep = ";"
            counts = {cand: sample.count(cand) for cand in [";", ",", "\t", "|"]}
            if max(counts.values()) > 0:
                sep = max(counts, key=counts.get)
            df = pd.read_csv(io.BytesIO(raw_bytes), sep=sep, header=None, engine="python", encoding="utf-8-sig", on_bad_lines="skip")
        else:
            df = pd.read_excel(io.BytesIO(raw_bytes), header=None)
    except Exception:
        return None
    if df is None or df.empty or df.shape[1] < 2:
        return None
    out = pd.DataFrame()
    out["telefone"] = df.iloc[:, 0].apply(_normalize_scalar_text)
    out["nome_cliente"] = df.iloc[:, 1].apply(_normalize_scalar_text)
    out["msg_2"] = df.iloc[:, 2].apply(_normalize_scalar_text) if df.shape[1] > 2 else ""
    out["msg_3"] = df.iloc[:, 3].apply(_normalize_scalar_text) if df.shape[1] > 3 else ""
    out["msg_4"] = df.iloc[:, 4].apply(_normalize_scalar_text) if df.shape[1] > 4 else ""
    out["arquivo"] = str(name or "")
    out["data_envio"] = _extract_date_from_filename(name)
    out = out[(out["telefone"] != "") | (out["nome_cliente"] != "")].copy()
    return out


def _build_previous_message_history(uploaded_files) -> pd.DataFrame:
    rows = []
    for f in uploaded_files or []:
        try:
            raw = f.getvalue()
        except Exception:
            continue
        df = _read_previous_message_file(getattr(f, "name", ""), raw)
        if df is None or df.empty:
            continue
        rows.append(df)
    if not rows:
        return pd.DataFrame(columns=[
            "nome_key", "qtde_envios_anteriores", "ultima_data_envio",
            "ultima_msg_2", "ultima_msg_3", "ultima_msg_4"
        ])
    hist = pd.concat(rows, ignore_index=True)
    hist["nome_key"] = hist["nome_cliente"].apply(_normalize_person_key)
    hist = hist[hist["nome_key"] != ""].copy()
    hist["data_envio"] = pd.to_datetime(hist["data_envio"], errors="coerce")
    hist = hist.sort_values(["nome_key", "data_envio", "arquivo"])
    agg = hist.groupby("nome_key", as_index=False).agg(
        qtde_envios_anteriores=("nome_key", "size"),
        ultima_data_envio=("data_envio", "max"),
    )
    last_rows = hist.dropna(subset=["data_envio"]).sort_values(["nome_key", "data_envio"]).drop_duplicates("nome_key", keep="last")
    if last_rows.empty:
        last_rows = hist.sort_values(["nome_key"]).drop_duplicates("nome_key", keep="last")
    last_rows = last_rows[["nome_key", "msg_2", "msg_3", "msg_4"]].rename(columns={
        "msg_2": "ultima_msg_2",
        "msg_3": "ultima_msg_3",
        "msg_4": "ultima_msg_4",
    })
    out = agg.merge(last_rows, on="nome_key", how="left")
    out["ultima_data_envio"] = pd.to_datetime(out["ultima_data_envio"], errors="coerce").dt.strftime("%d/%m/%Y").fillna("")
    for col in ["ultima_msg_2", "ultima_msg_3", "ultima_msg_4"]:
        out[col] = out[col].fillna("")
    return out


C6_SITE_URL = "https://www.c6bank.com.br/"


def _short_followup_pendency(value) -> str:
    txt = _normalize_text_value(value)
    if not txt:
        return "documento pendente"
    txt = re.sub(r"(?i)<br\s*/?>", "; ", txt)
    txt = re.sub(r"\s+", " ", txt).strip(" ;.")
    replacements = {
        "Procuracao": "Procuração",
        "procuracao": "procuração",
        "orgao": "órgão",
        "eleicao": "eleição",
        "instituicoes": "instituições",
        "informacoes": "informações",
        "socios": "sócios",
        "administracao": "administração",
        "representacao": "representação",
        "razao social": "razão social",
        "validacao": "validação",
        "publica": "pública",
        "legivel": "legível",
        "tambem": "também",
        "necessario": "necessário",
    }
    for src, dst in replacements.items():
        txt = txt.replace(src, dst)
    parts = [p.strip(" ;.") for p in re.split(r";+", txt) if p.strip(" ;.")]
    if parts:
        txt = "; ".join(parts[:2])
    if len(txt) > 150:
        txt = txt[:147].rsplit(" ", 1)[0].rstrip(" ,;.") + "..."
    return txt or "documento pendente"


def _lead_followup_strategy(row: dict) -> dict:
    dias = int(row.get("dias_desde_cadastro") or 0)
    status = _normalize_person_key(row.get("status_abertura_conta", ""))
    pend = _normalize_person_key(row.get("pendencias", ""))
    pend_curta = _short_followup_pendency(row.get("pendencias", ""))
    envios = int(row.get("qtde_envios_anteriores") or 0)
    tem_conta = str(row.get("abriu_conta_flag", "NÃO")).upper() == "SIM"

    if tem_conta:
        return {
            "foco_dia": "Sem ação",
            "objetivo": "Cliente já abriu conta",
            "justificativa": "Sem necessidade de nova abordagem de abertura.",
            "var_2": "Identificamos que a sua conta C6 Empresas já foi aberta, então não é necessário seguir com esta etapa.",
            "var_3": "Se precisar de apoio nos próximos passos, temos uma pessoa disponível para auxiliar você.",
            "var_4": f"Para baixar o app com segurança, acesse o site oficial do C6: {C6_SITE_URL}",
        }

    if "REFAZER" in status or "REFAZER" in pend or "PROCESSO" in pend:
        retomada = (
            "Como já falamos antes, hoje o foco é retomar o ponto parado e evitar novo atraso."
            if envios > 0 else
            "O melhor caminho agora é refazer a abertura com acompanhamento para evitar retrabalho."
        )
        return {
            "foco_dia": "Refazer processo",
            "objetivo": "Retomar a abertura imediatamente",
            "justificativa": "O cliente já demonstrou interesse, mas precisa reiniciar ou corrigir a abertura para concluir a conta.",
            "var_2": "Verificamos que a sua solicitação precisa ser retomada para concluir a abertura da conta C6 Empresas.",
            "var_3": retomada,
            "var_4": f"Responda esta mensagem para uma pessoa auxiliar você. Baixe o app pelo site oficial: {C6_SITE_URL}",
        }

    if any(term in status for term in ["DOCUMENTO", "ENVIAR"]) or any(term in pend for term in ["DOCUMENTO", "CONTRATO", "SOCIETARIO", "ASSINATURA", "PROCURACAO"]):
        continuidade = (
            "Como este ponto já foi sinalizado, queremos ajudar você a enviar o documento correto e destravar a abertura."
            if envios > 0 else
            "Com essa pendência resolvida, a sua solicitação pode avançar com muito mais agilidade."
        )
        return {
            "foco_dia": "Pendência documental",
            "objetivo": "Fazer o cliente enviar o documento pendente",
            "justificativa": "Há uma pendência objetiva que desbloqueia a abertura; esta é a ação mais rápida para converter.",
            "var_2": f"Sua abertura C6 Empresas está em andamento. Pendência: {pend_curta}.",
            "var_3": continuidade,
            "var_4": f"Responda esta mensagem para uma pessoa auxiliar você. Baixe o app pelo site oficial: {C6_SITE_URL}",
        }

    if dias <= 3:
        abordagem = (
            "Como o cadastro é recente, podemos acompanhar você desde o início para a abertura não ficar parada."
            if envios == 0 else
            "Dando continuidade ao contato anterior, podemos seguir com apoio para você concluir sem perder tempo."
        )
        return {
            "foco_dia": "Abertura assistida",
            "objetivo": "Converter rapidamente enquanto o lead está quente",
            "justificativa": "Lead recente tende a responder melhor a uma abordagem de apoio e conveniência.",
            "var_2": "Sua indicação para abertura da conta C6 Empresas já está disponível e podemos auxiliar você nesse processo com acompanhamento direto.",
            "var_3": abordagem,
            "var_4": f"Responda esta mensagem para uma pessoa auxiliar você. Baixe o app pelo site oficial: {C6_SITE_URL}",
        }

    if dias <= 7:
        var_2 = (
            "Dando sequência ao nosso contato, sua abertura C6 Empresas ainda pode avançar com apoio direto."
            if envios > 0 else
            "A conta C6 Empresas pode apoiar sua rotina financeira com mais organização, praticidade e integração bancária."
        )
        var_3 = (
            "Hoje o foco é tirar dúvidas e manter o processo em movimento, para a abertura não ficar parada."
            if envios > 0 else
            "Você pode contar com uma pessoa para auxiliar no passo a passo e concluir a abertura com mais segurança."
        )
        return {
            "foco_dia": "Benefícios da conta",
            "objetivo": "Reforçar valor percebido e destravar a abertura",
            "justificativa": "Após os primeiros dias, destacar vantagens concretas aumenta a chance de conversão.",
            "var_2": var_2,
            "var_3": var_3,
            "var_4": f"Responda esta mensagem para seguirmos com você. Baixe o app pelo site oficial: {C6_SITE_URL}",
        }

    if dias <= 11:
        var_2 = (
            "Estamos retomando sua abertura C6 Empresas porque ainda dá tempo de concluir com orientação objetiva."
            if envios > 0 else
            "Sua abertura C6 Empresas ainda pode ser concluída com suporte da nossa equipe."
        )
        var_4 = (
            f"Responda esta mensagem e seguimos para o próximo passo. Baixe o app pelo site oficial: {C6_SITE_URL}"
            if envios > 0 else
            f"Responda esta mensagem para priorizarmos seu atendimento. Baixe o app pelo site oficial: {C6_SITE_URL}"
        )
        return {
            "foco_dia": "Urgência comercial",
            "objetivo": "Retomar engajamento antes de esfriar",
            "justificativa": "Lead já recebeu contato e precisa de gatilho de prioridade para voltar ao fluxo.",
            "var_2": var_2,
            "var_3": "Quanto antes a conta for aberta, antes você avança para os benefícios e uso da estrutura bancária.",
            "var_4": var_4,
        }

    if envios >= 2:
        return {
            "foco_dia": "Última tentativa útil",
            "objetivo": "Gerar resposta objetiva do cliente",
            "justificativa": "Cliente já recebeu abordagens anteriores; agora a ação precisa ser mais direta e conclusiva.",
            "var_2": "Estamos fazendo um último contato sobre a sua abertura de conta C6 Empresas para verificar se você ainda tem interesse em concluir essa etapa.",
            "var_3": "Se houver qualquer dificuldade, temos uma pessoa disponível para auxiliar você de forma rápida.",
            "var_4": f"Para seguir, responda esta mensagem. Baixe o app pelo site oficial: {C6_SITE_URL}",
        }

    return {
        "foco_dia": "Retomada final",
        "objetivo": "Concluir abertura ainda dentro da janela útil",
        "justificativa": "O lead está perto do limite operacional de abordagem e ainda vale um contato objetivo.",
        "var_2": "Sua abertura de conta C6 Empresas ainda pode ser concluída, e nossa equipe pode apoiar você nesta etapa final.",
        "var_3": "A ideia é facilitar a conclusão com orientação prática e uma pessoa disponível para auxiliar.",
        "var_4": f"Se desejar seguir, responda esta mensagem. Baixe o app pelo site oficial: {C6_SITE_URL}",
    }


REPORT_LABELS = {
    "c6_act_operadores": ("Ranking ACT - Indicadores", "ranking_act_indicadores"),
    "c6_act_faixa": ("Conversão ACT por Perfil de Empresa", "conversao_act_perfil_empresa"),
    "c6_act_analitico": ("Analítico ACT - Indicações e Aberturas", "analitico_act_indicacoes_aberturas"),
    "c6_oco_operadores": ("Ranking OCO - Abertura de Contas", "ranking_oco_abertura_contas"),
    "c6_bko_summary": ("Resumo BKO - Aging Operacional", "resumo_bko_aging_operacional"),
    "c6_oco_analitico": ("Analítico OCO - Abertura de Contas", "analitico_oco_abertura_contas"),
    "c6_oql_operadores": ("Ranking OQL - Qualificadores", "ranking_oql_qualificadores"),
    "c6_oql_estagio": ("Qualificação por Estágio M0 M1 M2", "qualificacao_estagio_m0_m1_m2"),
    "c6_oql_pix_wallet_pay": ("Resumo OQL - Pix Wallet C6 Pay", "resumo_oql_pix_wallet_c6pay"),
    "c6_oql_analitico": ("Analítico OQL - Qualificadores", "analitico_oql_qualificadores"),
    "bko_5mais_dias": ("Analítico BKO - 5 Dias Úteis", "analitico_bko_5_dias_uteis"),
    "painel_comparativo_diario": ("Comparativo Diário - Indicadores C6", "comparativo_diario_indicadores_c6"),
    "painel_comparativo_mesmo_dia": ("Comparativo por Dia do Mês", "comparativo_dia_mes"),
    "painel_base_mes": ("Base Consolidada do Mês", "base_consolidada_mes"),
    "painel_aberturas_dia": ("Aberturas por Dia", "aberturas_por_dia"),
    "painel_fundacao_mes": ("Fundações por Mês", "fundacoes_por_mes"),
    "painel_pix_status": ("Pix e Status da Carteira", "pix_status_carteira"),
    "painel_qualificados_dia": ("Qualificação por BR e Nível", "qualificacao_br_nivel"),
    "painel_comparativo_receita": ("Comparativo de Receita - Regras", "comparativo_receita_regras"),
    "painel_quadro_antigo_novo": ("Quadro Comparativo - Regra Antiga x Nova", "quadro_comparativo_regra_antiga_nova"),
    "painel_foco_vigente_resumo": ("Resumo de Foco Comercial", "resumo_foco_comercial"),
    "painel_foco_proximo_mes": ("Foco Comercial por Cliente", "foco_comercial_cliente"),
    "painel_campanha_tri": ("Campanha 2º Trimestre - Acompanhamento", "campanha_2tri_acompanhamento"),
    "painel_receita_liquida": ("Receita Líquida", "receita_liquida"),
    "painel_remuneracao_antiga": ("Remuneração Mensal - Regra Antiga", "remuneracao_mensal_regra_antiga"),
    "supervisor_indicadores": ("Meta Supervisor - Indicadores", "meta_supervisor_indicadores"),
    "supervisor_evolucao_diaria": ("Meta Supervisor - Evolução Diária", "meta_supervisor_evolucao_diaria"),
    "campanhas_meta_mensal": ("Campanhas Meta - Consolidado Mensal", "campanhas_meta_consolidado_mensal"),
    "campanhas_meta_diario": ("Campanhas Meta - Consolidado Diário", "campanhas_meta_consolidado_diario"),
    "leads_prazo_abertura": ("Leads - Prazo de Abertura", "leads_prazo_abertura"),
    "leads_carteira_pendente": ("Leads - Carteira Pendente", "leads_carteira_pendente"),
    "leads_analitico_prazo": ("Analítico de Leads - Prazo de Abertura", "analitico_leads_prazo_abertura"),
    "leads_followup_1a15_dias": ("Analítico Follow-up - 1 a 15 Dias", "analitico_followup_1a15_dias"),
    "leads_clientes_ura": ("Clientes URA - Efetividade", "clientes_ura_efetividade"),
    "leads_clientes_ura_analitico": ("Analítico URA - Clientes e Conversão", "analitico_ura_clientes_conversao"),
    "leads_funil_avanco": ("Funil C6 - Avanço do Cliente", "funil_c6_avanco_cliente"),
    "leads_funil_tempo": ("Funil C6 - Tempos Médios", "funil_c6_tempos_medios"),
    "leads_status_hist": ("Leads - Histórico de Status", "leads_historico_status"),
    "mensagens_abertura_preview": ("Mensagens - Abertura de Contas", "mensagens_abertura_contas"),
}


def _report_display_info(filename_prefix: str) -> Tuple[str, str]:
    label, fname = REPORT_LABELS.get(str(filename_prefix), (str(filename_prefix).replace("_", " ").title(), str(filename_prefix)))
    safe_fname = re.sub(r"[^A-Za-z0-9_\-]+", "_", fname).strip("_").lower() or "relatorio"
    return label, safe_fname


def _downloads_enabled() -> bool:
    return bool(st.session_state.get("_prepare_downloads", False))


def render_downloadable_table(df_display, key_prefix: str, filename_prefix: str, raw_df=None, hide_index: bool = True, use_container_width: bool = True):
    base = raw_df if raw_df is not None else getattr(df_display, "data", df_display)
    if isinstance(base, pd.Series):
        base = base.to_frame()
    report_label, safe_filename = _report_display_info(filename_prefix)
    preview = df_display
    preview_note = ""
    try:
        if isinstance(df_display, pd.DataFrame) and len(df_display) > 1000:
            preview = df_display.head(1000)
            preview_note = f"Prévia limitada a 1.000 linhas. O download contém {br_int(len(base))} registros."
    except Exception:
        pass
    st.dataframe(preview, use_container_width=use_container_width, hide_index=hide_index)
    if preview_note:
        st.caption(preview_note)
    if isinstance(base, pd.DataFrame):
        if not _downloads_enabled():
            st.caption("Download disponível ao ativar Preparar downloads.")
            return
        seq = st.session_state.get("_dl_btn_seq", 0)
        st.session_state["_dl_btn_seq"] = seq + 1
        unique_key = f"dl_{key_prefix}_{seq}"
        st.download_button(
            f"Baixar Excel - {report_label}",
            data=_to_excel_bytes({"Tabela": base}),
            file_name=f"{safe_filename}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=unique_key,
            use_container_width=False,
            help=f"Baixar {report_label} em Excel",
        )


def read_excel_any(file_bytes: bytes) -> pd.DataFrame:
    return _read_excel_any_cached(bytes(file_bytes))


@st.cache_data(show_spinner=False)
def _read_excel_any_cached(file_bytes: bytes) -> pd.DataFrame:
    return pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")


def _single_visible_tab(options: List[str], key: str, default: Optional[str] = None):
    opts = [o for o in (options or []) if o]
    if not opts:
        return "", {}
    current = st.session_state.get(key)
    if current not in opts:
        current = default if default in opts else opts[0]
        st.session_state[key] = current
    selected = st.radio(
        "Selecione uma aba",
        opts,
        index=opts.index(current),
        key=key,
        horizontal=True,
        label_visibility="collapsed",
    )
    return selected, {selected: st.container()}


def _safe_file_signature(path: str) -> tuple:
    try:
        stt = os.stat(path)
        return (os.path.basename(path), int(stt.st_mtime_ns), int(stt.st_size))
    except Exception:
        return (os.path.basename(path), 0, 0)


def _temp_import_state(keyword: str) -> list[tuple]:
    return [_safe_file_signature(path) for path in _temp_import_files_by_keyword(keyword)]


def _load_temp_import_daily_df(keyword: str, target_day: Optional[dt.date] = None) -> Tuple[Optional[pd.DataFrame], str]:
    best_df = None
    best_name = ""
    best_path = ""
    for path in _temp_import_files_by_keyword(keyword):
        try:
            _, mtime_ns, size = _safe_file_signature(path)
            df = _read_excel_path_cached(path, mtime_ns, size)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        report_day = detect_report_day_from_df(df)
        if target_day is not None:
            if report_day != target_day:
                continue
            return df.copy(), os.path.basename(path)
        if report_day is None:
            continue
        if best_df is None:
            best_df = df.copy()
            best_name = os.path.basename(path)
            best_path = path
            continue
        best_day = detect_report_day_from_df(best_df)
        if best_day is None or report_day > best_day:
            best_df = df.copy()
            best_name = os.path.basename(path)
            best_path = path
        elif best_day == report_day and os.path.basename(path) > os.path.basename(best_path):
            best_df = df.copy()
            best_name = os.path.basename(path)
            best_path = path
    return best_df, best_name


def _panel_c6_refresh_signature() -> str:
    meta = local_json_load(C6_DAILY_IMPORT_META, default={}) or {}
    payload = {
        "meta": meta,
        "visao_files": _temp_import_state("visao"),
        "leads_files": _temp_import_state("leads"),
        # Bump quando mudar lógica da cartilha nova / painel (invalida cache em disco).
        "_cartilha_nova_engine": REMUN_ENGINE_VERSION,
    }
    try:
        return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        return str(payload)


def _load_panel_c6_cached_df(path: str) -> pd.DataFrame:
    payload = local_json_load(path, default={}) or {}
    df = _df_from_store_payload(payload)
    if df is None:
        return pd.DataFrame()
    return df


def _save_panel_c6_cached_df(path: str, df: pd.DataFrame):
    try:
        local_json_save(path, _df_to_store_payload(df if df is not None else pd.DataFrame()))
    except Exception:
        pass


def _patch_panel_cache_row(path: str, month_key: str, row: list):
    payload = local_json_load(path, default={}) or {}
    data = payload.get("data", []) if isinstance(payload, dict) else []
    if not isinstance(data, list):
        data = []
    updated = False
    for existing in data:
        if existing and str(existing[0]) == str(month_key):
            existing[:] = row
            updated = True
            break
    if not updated:
        data.append(row)
    payload["data"] = data
    payload["index"] = list(range(len(data)))
    local_json_save(path, payload)


@st.cache_data(show_spinner=False)
def _read_excel_path_cached(path: str, mtime_ns: int, size: int) -> pd.DataFrame:
    return pd.read_excel(path, engine="openpyxl")


def _save_daily_import_cache(kind: str, file_name: str, raw_bytes: bytes):
    kind_key = str(kind or "").strip().lower()
    if kind_key == "visao":
        cache_path = C6_DAILY_VISAO_CACHE
    elif kind_key == "lct":
        cache_path = C6_DAILY_LCT_CLOUD_CACHE if "firebase" in st.secrets else C6_DAILY_LCT_CACHE
    else:
        cache_path = C6_DAILY_LEADS_CACHE
    if "firebase" in st.secrets:
        saved_cache = False
        try:
            if kind_key == "lct":
                df_cache = _read_lct_file_any(file_name, raw_bytes)
                df_cache = _compact_lct_cache_df(df_cache)
            elif str(file_name or "").lower().endswith(".csv"):
                sample = raw_bytes[:200_000].decode("utf-8-sig", errors="replace")
                candidates = [";", ",", "\t", "|"]
                counts = {sep: sample.count(sep) for sep in candidates}
                sep = max(counts, key=counts.get) if counts else ","
                if counts.get(sep, 0) <= 0:
                    sep = ","
                df_cache = pd.read_csv(io.BytesIO(raw_bytes), sep=sep, engine="python", on_bad_lines="skip", encoding="utf-8-sig")
            else:
                df_cache = read_excel_any(raw_bytes)
            if df_cache is not None:
                saved_cache = bool(safe_json_save(cache_path, _df_to_store_payload(df_cache)))
        except Exception:
            saved_cache = False
        if not saved_cache:
            return False
    else:
        with open(cache_path, "wb") as f:
            f.write(raw_bytes)
        saved_cache = True
    meta = local_json_load(C6_DAILY_IMPORT_META, default={}) or {}
    meta[kind_key] = {
        "name": str(file_name or "").strip(),
        "cached_at": dt.datetime.now().isoformat(),
    }
    saved_meta = bool(local_json_save(C6_DAILY_IMPORT_META, meta))
    try:
        _write_daily_temp_import_copy(file_name, raw_bytes)
    except Exception:
        pass
    return saved_cache if "firebase" in st.secrets else saved_meta


def _daily_import_cached_at(meta: dict, kind_key: str) -> dt.datetime:
    try:
        info = (meta or {}).get(kind_key) or {}
        ts = pd.to_datetime(info.get("cached_at"), errors="coerce")
        if pd.isna(ts):
            return dt.datetime.min
        return ts.to_pydatetime()
    except Exception:
        return dt.datetime.min


def _meta_cached_at(meta: dict) -> float:
    try:
        ts = pd.to_datetime((meta or {}).get("cached_at"), errors="coerce")
        if pd.isna(ts):
            return -1.0
        return float(ts.timestamp())
    except Exception:
        return -1.0


def _daily_payload_max_data_base(payload) -> dt.datetime:
    if not isinstance(payload, dict):
        return dt.datetime.min
    cols = payload.get("columns")
    data = payload.get("data")
    if not isinstance(cols, list) or not isinstance(data, list):
        return dt.datetime.min
    target_idx = None
    for idx, col in enumerate(cols):
        key = _normalize_person_key(col)
        if key in {"DATA BASE", "DATA_BASE", "DT BASE", "DT_BASE", "DATA LCT", "DATA_LCT"} or key.endswith("DATA BASE"):
            target_idx = idx
            break
    if target_idx is None:
        return dt.datetime.min
    max_ts = pd.NaT
    sample_rows = data[-500:] if len(data) > 500 else data
    for row in sample_rows:
        if not isinstance(row, list) or target_idx >= len(row):
            continue
        ts = pd.to_datetime(row[target_idx], errors="coerce", dayfirst=True)
        if pd.notna(ts) and (pd.isna(max_ts) or ts > max_ts):
            max_ts = ts
    if pd.isna(max_ts):
        return dt.datetime.min
    try:
        return max_ts.to_pydatetime()
    except Exception:
        return dt.datetime.min


def _load_cloud_daily_import_payload(cache_path: str, kind_key: str):
    cache_doc = _fs_doc_id_from_path(cache_path)
    meta_doc = _fs_doc_id_from_path(C6_DAILY_IMPORT_META)
    session_payloads = st.session_state.get("_cloud_session_payloads", {})
    if isinstance(session_payloads, dict) and cache_doc in session_payloads:
        meta = session_payloads.get(meta_doc)
        if not isinstance(meta, dict):
            meta = safe_json_load(C6_DAILY_IMPORT_META, default={}) or {}
        return session_payloads.get(cache_doc), meta, "Importação diária (sessão atual)"

    cloud_payload = _fs_load_payload(cache_doc, _MISSING)
    cloud_meta = _fs_load_payload(meta_doc, _MISSING)
    if cloud_meta is _MISSING or not isinstance(cloud_meta, dict):
        cloud_meta = {}

    bundled_payload = _bundled_seed_payload(cache_doc, _MISSING)
    bundled_meta = _bundled_seed_payload(meta_doc, {})
    if not isinstance(bundled_meta, dict):
        bundled_meta = {}

    cloud_ts = _daily_import_cached_at(cloud_meta, kind_key)
    bundled_ts = _daily_import_cached_at(bundled_meta, kind_key)
    cloud_data_day = _daily_payload_max_data_base(cloud_payload)
    bundled_data_day = _daily_payload_max_data_base(bundled_payload)

    if (
        bundled_payload is not _MISSING
        and (
            cloud_payload is _MISSING
            or bundled_data_day > cloud_data_day
            or (bundled_data_day == cloud_data_day and cloud_ts != dt.datetime.min and bundled_ts > cloud_ts)
        )
    ):
        return bundled_payload, bundled_meta, "Importação diária (pacote publicado)"
    if cloud_payload is not _MISSING:
        return cloud_payload, cloud_meta, "Importação diária (cache nuvem)"
    return None, {}, ""


def _filename_has_required_keyword(file_name: str, keyword: str) -> bool:
    def _clean_text(v: str) -> str:
        txt = str(v or "").strip().lower()
        txt = unicodedata.normalize("NFKD", txt).encode("ascii", "ignore").decode("ascii")
        return txt

    name = _clean_text(file_name)
    key = _clean_text(keyword)
    return bool(name) and bool(key) and key in name


def _load_daily_import_cache(kind: str):
    kind_key = str(kind or "").strip().lower()
    if kind_key == "lct" and st.session_state.get("c6_daily_lct_df") is not None:
        try:
            return st.session_state["c6_daily_lct_df"].copy(), str(st.session_state.get("c6_daily_lct_df__name") or ""), "Resumo LCT (sessão atual)"
        except Exception:
            pass
    if kind_key == "visao":
        cache_path = C6_DAILY_VISAO_CACHE
    elif kind_key == "lct":
        cache_path = C6_DAILY_LCT_CLOUD_CACHE if "firebase" in st.secrets else C6_DAILY_LCT_CACHE
    else:
        cache_path = C6_DAILY_LEADS_CACHE
    if "firebase" in st.secrets:
        payload, meta, origin = _load_cloud_daily_import_payload(cache_path, kind_key)
        if kind_key == "lct" and not payload:
            payload, meta, origin = _load_cloud_daily_import_payload(C6_DAILY_LCT_CACHE, kind_key)
        if payload:
            df = _df_from_store_payload(payload)
            info = meta.get(kind_key) or {}
            return df, str(info.get("name") or ""), origin
        return None, "", ""
    if not os.path.exists(cache_path):
        return None, "", ""
    try:
        with open(cache_path, "rb") as f:
            raw = f.read()
        info = (local_json_load(C6_DAILY_IMPORT_META, default={}) or {}).get(kind_key) or {}
        file_name = str(info.get("name") or "")
        if file_name.lower().endswith(".csv"):
            sample = raw[:200_000].decode("utf-8-sig", errors="replace")
            candidates = [";", ",", "\t", "|"]
            counts = {sep: sample.count(sep) for sep in candidates}
            sep = max(counts, key=counts.get) if counts else ","
            if counts.get(sep, 0) <= 0:
                sep = ","
            df = pd.read_csv(io.BytesIO(raw), sep=sep, engine="python", on_bad_lines="skip", encoding="utf-8-sig")
        elif kind_key == "lct":
            df = _read_lct_file_any(file_name, raw)
        else:
            df = read_excel_any(raw)
        meta = local_json_load(C6_DAILY_IMPORT_META, default={}) or {}
        info = meta.get(kind_key) or {}
        return df, str(info.get("name") or ""), "Importação diária (cache local)"
    except Exception:
        return None, "", ""


def _save_ops_import_cache(file_name: str, raw_bytes: bytes):
    if "firebase" in st.secrets:
        try:
            fake_upload = type("UploadedCache", (), {"getvalue": lambda self: raw_bytes, "name": str(file_name or "c6_operacao_cache.csv")})()
            df_cache = _read_ops_file(fake_upload)
            if df_cache is not None and not df_cache.empty:
                safe_json_save(C6_OPS_CACHE, _df_to_store_payload(df_cache))
        except Exception:
            pass
    else:
        with open(C6_OPS_CACHE, "wb") as f:
            f.write(raw_bytes)
    local_json_save(C6_OPS_CACHE_META, {
        "name": str(file_name or "").strip(),
        "cached_at": dt.datetime.now().isoformat(),
    })


def _clear_c6_operacao_runtime_cache():
    for key in ["c6_operacao_last_signature", "c6_operacao_last_result", "c6_operacao_last_result__ts"]:
        st.session_state.pop(key, None)


def _load_ops_import_cache():
    meta = local_json_load(C6_OPS_CACHE_META, default={}) or {}
    if "firebase" in st.secrets:
        payload = safe_json_load(C6_OPS_CACHE, default=None)
        if payload:
            return _df_from_store_payload(payload), str(meta.get("name") or "")
        return None, ""
    if not os.path.exists(C6_OPS_CACHE):
        return None, ""
    try:
        with open(C6_OPS_CACHE, "rb") as f:
            raw = f.read()
        df = _read_ops_file(type("CachedUpload", (), {"getvalue": lambda self: raw, "name": str(meta.get("name") or "c6_operacao_cache.csv")})())
        return df, str(meta.get("name") or "")
    except Exception:
        return None, ""


def _cloud_payload_source_name(target_name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", os.path.basename(str(target_name or "")))
    return f"cloud_payload_{safe}.json"


def _write_cloud_cache_payload(path: str, df: Optional[pd.DataFrame]) -> Optional[dict]:
    if df is None or df.empty:
        return None
    source_name = _cloud_payload_source_name(os.path.basename(path))
    source_path = os.path.join(DATA_DIR, source_name)
    try:
        with open(source_path, "w", encoding="utf-8") as f:
            json.dump(_df_to_store_payload(df), f, ensure_ascii=False, separators=(",", ":"))
        return {"source": source_name, "target": os.path.basename(path)}
    except Exception:
        return None


def _prepare_cloud_seed_from_local_data() -> List:
    seed_path = os.path.join(DATA_DIR, "cloud_seed_version.json")
    try:
        with open(seed_path, "r", encoding="utf-8") as f:
            seed = json.load(f)
        files = list(seed.get("files") or [])
    except Exception:
        files = []

    by_key = {}
    for entry in files:
        key = os.path.basename(str(entry.get("target") or entry.get("source") or "")) if isinstance(entry, dict) else os.path.basename(str(entry or ""))
        if key and not key.startswith("cloud_payload_"):
            by_key[key] = entry

    for name in os.listdir(DATA_DIR):
        if (
            name.endswith(".json")
            and not name.startswith("cloud_payload_")
            and not name.startswith("cloud_seed_version")
            and ".corrompido_" not in name
            and ".backup" not in name
        ):
            by_key.setdefault(name, name)

    cache_entries = []
    try:
        df_visao, _, _ = _load_daily_import_cache("visao")
        entry = _write_cloud_cache_payload(C6_DAILY_VISAO_CACHE, df_visao)
        if entry:
            cache_entries.append(entry)
    except Exception:
        pass
    try:
        df_leads, _, _ = _load_daily_import_cache("leads")
        entry = _write_cloud_cache_payload(C6_DAILY_LEADS_CACHE, df_leads)
        if entry:
            cache_entries.append(entry)
    except Exception:
        pass
    try:
        df_lct, _, _ = _load_daily_import_cache("lct")
        df_lct = _compact_lct_cache_df(df_lct)
        entry = _write_cloud_cache_payload(C6_DAILY_LCT_CLOUD_CACHE, df_lct)
        if entry:
            cache_entries.append(entry)
    except Exception:
        pass
    try:
        df_ops, _ = _load_ops_import_cache()
        entry = _write_cloud_cache_payload(C6_OPS_CACHE, df_ops)
        if entry:
            cache_entries.append(entry)
    except Exception:
        pass

    for entry in cache_entries:
        by_key[os.path.basename(str(entry.get("target") or entry.get("source") or ""))] = entry
    return list(by_key.values())


def _sync_local_data_to_cloud_seed(reason: str = "") -> Tuple[bool, str]:
    """Publica os dados locais versionados para o app online reidratar o Firestore."""
    if "firebase" in st.secrets:
        return False, "O app já está usando a base em nuvem."
    if not os.path.isdir(os.path.join(APP_DIR, ".git")):
        return False, "Repositório Git não encontrado."
    try:
        files = _prepare_cloud_seed_from_local_data()
        version = dt.datetime.now().strftime("%Y-%m-%d-%H%M%S")
        seed = {
            "version": version,
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "reason": str(reason or "sync-local"),
            "files": files,
        }
        seed_path = os.path.join(DATA_DIR, "cloud_seed_version.json")
        with open(seed_path, "w", encoding="utf-8") as f:
            json.dump(seed, f, ensure_ascii=False, indent=2)

        paths = [os.path.join("data_store", "cloud_seed_version.json")]
        for entry in files:
            source = entry.get("source") if isinstance(entry, dict) else entry
            source = os.path.basename(str(source or ""))
            if source:
                paths.append(os.path.join("data_store", source))
        subprocess.run(["git", "add", "-f", *paths], cwd=APP_DIR, check=True, capture_output=True, text=True)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet", "--", "data_store"], cwd=APP_DIR)
        if diff.returncode == 0:
            return False, "Sem mudanças de dados para publicar."
        label = dt.datetime.now().strftime("%d/%m/%Y %H:%M")
        subprocess.run(["git", "commit", "-m", f"Atualizar dados C6 online {label}"], cwd=APP_DIR, check=True, capture_output=True, text=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=APP_DIR, check=True, capture_output=True, text=True)
        return True, "Dados locais publicados para o app online."
    except Exception as exc:
        return False, f"Falha ao publicar dados locais: {exc}"


def _truthy_flag(value) -> bool:
    if value is None:
        txt = ""
    else:
        try:
            txt = "" if pd.isna(value) else str(value).strip().upper()
        except Exception:
            txt = str(value).strip().upper()
    return txt in {"1", "S", "SIM", "TRUE", "ATIVA", "ATIVO", "YES"}


def _row_first_value(row, names: List[str], default=""):
    for name in names:
        try:
            if name in row.index:
                val = row.get(name)
                if val is None:
                    continue
                try:
                    if pd.isna(val):
                        continue
                except Exception:
                    pass
                if str(val).strip() != "":
                    return val
        except Exception:
            pass
        try:
            val = row.get(name)
            if val is None:
                continue
            try:
                if pd.isna(val):
                    continue
            except Exception:
                pass
            if str(val).strip() != "":
                return val
        except Exception:
            pass
    return default


def _wallet_raw_from_row(row) -> str:
    return str(_row_first_value(row, ["FL_WALLET_CADASTRADA", "WALLET", "FL_WALLET", "CARTAO_WALLET"], "") or "").strip().upper()


def _persist_visao_funil_track(df_c6: pd.DataFrame):
    report_day = detect_report_day_from_df(df_c6)
    if report_day is None:
        report_day = detect_report_month_from_df(df_c6)
    if report_day is None:
        return

    day_txt = fmt_date(report_day)
    track = local_json_load(C6_DAILY_FUNIL_TRACK, default={}) or {}
    if COL_CNPJ not in df_c6.columns:
        return

    for _, row in df_c6.iterrows():
        cnpj = _normalize_cnpj_text(row.get(COL_CNPJ))
        if not cnpj:
            continue

        item = track.get(cnpj) or {}

        def _set_min_date(field_name: str, raw_value):
            d = pd.to_datetime(raw_value, errors="coerce")
            if pd.isna(d):
                return
            txt = fmt_date(d)
            old = _parse_br_date_text(item.get(field_name))
            if old is None or d.date() < old:
                item[field_name] = txt

        _set_min_date("dt_conta_criada", row.get("DT_CONTA_CRIADA"))
        _set_min_date("dt_entrega_cartao", row.get("DT_ENTREGA_CARTAO"))
        _set_min_date("dt_ativ_cartao_cred", row.get("DT_ATIV_CARTAO_CRED"))
        _set_min_date("dt_aprovacao_pay", row.get("DT_APROVACAO_PAY"))
        _set_min_date("dt_install_maq", row.get("DT_INSTALL_MAQ"))
        _set_min_date("dt_ativacao_pay", row.get("DT_ATIVACAO_PAY"))

        pix_raw = _pix_clean_value(row.get("CHAVES_PIX_FORTE", ""))
        if _pix_is_valid(pix_raw) and not item.get("pix_primeira_aparicao"):
            item["pix_primeira_aparicao"] = day_txt
        if _pix_has_cnpj(pix_raw) and not item.get("pix_cnpj_primeira_aparicao"):
            item["pix_cnpj_primeira_aparicao"] = day_txt

        wallet_raw = _wallet_raw_from_row(row)
        if _truthy_flag(wallet_raw) and not item.get("wallet_primeira_aparicao"):
            item["wallet_primeira_aparicao"] = day_txt

        if _truthy_flag(row.get("C6PAY_ATIVA_30")) and not item.get("c6pay_ativa30_primeira_aparicao"):
            item["c6pay_ativa30_primeira_aparicao"] = day_txt

        item["ultima_data_base"] = day_txt
        track[cnpj] = item

    local_json_save(C6_DAILY_FUNIL_TRACK, track)


def _persist_leads_cnpj_track(df_leads: pd.DataFrame):
    try:
        base = _extract_leads_base(df_leads)
    except Exception:
        return
    if base is None or base.empty:
        return

    track = local_json_load(C6_LEADS_CNPJ_TRACK, default={}) or {}
    for _, row in base.iterrows():
        cnpj = str(row.get("cnpj") or "").strip()
        if not cnpj:
            continue
        data_base = pd.to_datetime(row.get("data_base"), errors="coerce")
        if pd.isna(data_base):
            continue
        day_iso = data_base.date().isoformat()
        item = track.get(cnpj) or {"timeline": {}}
        timeline = item.get("timeline") if isinstance(item.get("timeline"), dict) else {}
        timeline[day_iso] = {
            "nome_cliente": str(row.get("nome_cliente") or ""),
            "data_hora_cadastro": fmt_date(pd.to_datetime(row.get("data_hora_cadastro"), errors="coerce")),
            "status_abertura_conta": str(row.get("status_abertura_conta") or ""),
            "status_final": str(row.get("status_final") or ""),
            "pendencias": str(row.get("pendencias") or ""),
            "dt_conta_aberta_leads": fmt_date(pd.to_datetime(row.get("dt_conta_aberta_leads"), errors="coerce")),
        }
        item["timeline"] = timeline
        track[cnpj] = item
    local_json_save(C6_LEADS_CNPJ_TRACK, track)


def _calc_bko_business_streak(cnpj: str, current_data_base) -> int:
    if not cnpj or pd.isna(current_data_base):
        return 0
    track = local_json_load(C6_LEADS_CNPJ_TRACK, default={}) or {}
    item = track.get(str(cnpj)) or {}
    timeline = item.get("timeline") if isinstance(item.get("timeline"), dict) else {}
    if not timeline:
        return 0

    target_status = "AGUARDAR ATUACAO MANUAL BKO"
    current_day = pd.Timestamp(current_data_base).date()
    days_present = {}
    for day_iso, payload in timeline.items():
        try:
            day = dt.date.fromisoformat(str(day_iso))
        except Exception:
            continue
        days_present[day] = _normalize_status_key((payload or {}).get("status_abertura_conta") or "")

    current_status = days_present.get(current_day)
    if current_status != target_status:
        return 0

    known_days = sorted([d for d in days_present.keys() if d <= current_day and d.weekday() < 5])
    if not known_days:
        return 0

    segment_start = current_day
    for day in reversed(known_days):
        status = days_present.get(day)
        if status != target_status:
            break
        segment_start = day

    bdays = pd.bdate_range(segment_start, current_day)
    days_count = int(len(bdays))
    if days_count < 5:
        payload = (timeline or {}).get(current_day.isoformat()) or {}
        cadastro = pd.to_datetime((payload or {}).get("data_hora_cadastro"), errors="coerce", dayfirst=True)
        if pd.notna(cadastro):
            days_count = max(days_count, int(len(pd.bdate_range(pd.Timestamp(cadastro).date(), current_day))))
    return int(days_count)


def _calc_bko_followup(cnpj: str, current_data_base) -> Dict[str, object]:
    empty = {
        "dias_uteis_bko": 0,
        "bucket_bko": "",
        "situacao_bko": "",
        "status_pos_5_dias": "",
        "data_saida_bko": "",
        "status_atual": "",
    }
    if not cnpj or pd.isna(current_data_base):
        return empty

    track = local_json_load(C6_LEADS_CNPJ_TRACK, default={}) or {}
    item = track.get(str(cnpj)) or {}
    timeline = item.get("timeline") if isinstance(item.get("timeline"), dict) else {}
    if not timeline:
        return empty

    target_status = "AGUARDAR ATUACAO MANUAL BKO"
    current_day = pd.Timestamp(current_data_base).date()
    days_present = {}
    payloads = {}
    for day_iso, payload in timeline.items():
        try:
            day = dt.date.fromisoformat(str(day_iso))
        except Exception:
            continue
        if day > current_day or day.weekday() >= 5:
            continue
        days_present[day] = _normalize_status_key((payload or {}).get("status_abertura_conta") or "")
        payloads[day] = payload if isinstance(payload, dict) else {}

    known_days = sorted(days_present.keys())
    if not known_days:
        return empty

    current_status = days_present.get(current_day, "")
    segments = []
    segment_start = None
    previous_day = None
    for day in known_days:
        status = days_present.get(day, "")
        if status == target_status:
            if segment_start is None:
                segment_start = day
        else:
            if segment_start is not None and previous_day is not None:
                days_count = int(len(pd.bdate_range(segment_start, previous_day)))
                if days_count >= 5:
                    segments.append({"start": segment_start, "end": previous_day, "days": days_count})
                segment_start = None
        previous_day = day

    if segment_start is not None and previous_day is not None:
        days_count = int(len(pd.bdate_range(segment_start, previous_day)))
        if days_count >= 5:
            segments.append({"start": segment_start, "end": previous_day, "days": days_count})

    if not segments:
        return {**empty, "status_atual": current_status}

    last_segment = segments[-1]
    status_pos = ""
    data_saida = ""
    situacao = "Em BKO 5+ dias"
    dias_bko = int(last_segment["days"])

    if current_status != target_status or current_day > last_segment["end"]:
        situacao = "Saiu do BKO após 5+ dias"
        for day in known_days:
            if day <= last_segment["end"]:
                continue
            status = days_present.get(day, "")
            if status != target_status:
                status_pos = status
                data_saida = fmt_date(day)
                break
        if not status_pos:
            status_pos = current_status

    return {
        "dias_uteis_bko": dias_bko,
        "bucket_bko": "5+ dias úteis",
        "situacao_bko": situacao,
        "status_pos_5_dias": status_pos,
        "data_saida_bko": data_saida,
        "status_atual": current_status,
    }


def _build_bko_followup_table(base: pd.DataFrame, oco: pd.DataFrame) -> pd.DataFrame:
    track = local_json_load(C6_LEADS_CNPJ_TRACK, default={}) or {}
    target_status = "AGUARDAR ATUACAO MANUAL BKO"
    rows = []

    for cnpj, item in track.items():
        timeline = item.get("timeline") if isinstance(item, dict) else {}
        if not isinstance(timeline, dict) or not timeline:
            continue

        events = []
        for day_iso, payload in timeline.items():
            if not isinstance(payload, dict):
                continue
            try:
                day = dt.date.fromisoformat(str(day_iso))
            except Exception:
                continue
            if day.weekday() >= 5:
                continue
            events.append((day, payload, _normalize_status_key(payload.get("status_abertura_conta", ""))))

        events = sorted(events, key=lambda x: x[0])
        if not events:
            continue

        segments = []
        i = 0
        while i < len(events):
            day, payload, status = events[i]
            if status != target_status:
                i += 1
                continue

            start_i = i
            while i + 1 < len(events) and events[i + 1][2] == target_status:
                i += 1
            end_i = i

            start_day = events[start_i][0]
            end_day = events[end_i][0]
            days_count = int(len(pd.bdate_range(start_day, end_day)))
            if days_count >= 5:
                exit_event = events[end_i + 1] if end_i + 1 < len(events) else None
                segments.append({
                    "start": start_day,
                    "end": end_day,
                    "days": days_count,
                    "exit": exit_event,
                })
            i += 1

        if not segments:
            latest_day, latest_payload, latest_status = events[-1]
            if latest_status != target_status:
                continue
            cadastro = pd.to_datetime((latest_payload or {}).get("data_hora_cadastro"), errors="coerce", dayfirst=True)
            if pd.isna(cadastro):
                continue
            start_day = pd.Timestamp(cadastro).date()
            days_count = int(len(pd.bdate_range(start_day, latest_day)))
            if days_count < 5:
                continue
            segments.append({
                "start": start_day,
                "end": latest_day,
                "days": days_count,
                "exit": None,
            })

        last_segment = segments[-1]
        latest_day, latest_payload, latest_status = events[-1]
        ativo = latest_status == target_status
        exit_event = last_segment.get("exit")
        status_pos = ""
        data_saida = ""
        if not ativo:
            if exit_event:
                status_pos = exit_event[2]
                data_saida = fmt_date(exit_event[0])
            else:
                status_pos = latest_status

        rows.append({
            "cnpj": str(cnpj),
            "nome_cliente": str(latest_payload.get("nome_cliente") or ""),
            "data_base": latest_day,
            "data_hora_cadastro": latest_payload.get("data_hora_cadastro", ""),
            "status_abertura_conta": latest_status,
            "status_bko": "Ativo" if ativo else "Inativo",
            "status_pos_5_dias": status_pos,
            "data_saida_bko": data_saida,
            "dias_uteis_bko": int(last_segment["days"]),
            "bucket_bko": "5+ dias úteis",
            "ultima_base_importada": fmt_date(latest_day),
            "data_inicio_bko": fmt_date(last_segment["start"]),
            "ultima_data_em_bko": fmt_date(last_segment["end"]),
            "pendencias": str(latest_payload.get("pendencias") or ""),
            "dt_conta_criada": latest_payload.get("dt_conta_aberta_leads", ""),
        })

    out = pd.DataFrame(rows)
    if out.empty:
        return pd.DataFrame(columns=[
            "nome_cliente", "cnpj", "operador", "data_acao", "data_base", "data_hora_cadastro",
            "status_abertura_conta", "status_bko", "status_pos_5_dias", "data_saida_bko",
            "dias_uteis_bko", "bucket_bko", "ultima_base_importada", "data_inicio_bko",
            "ultima_data_em_bko", "pendencias", "dt_conta_criada"
        ])

    base_info_cols = [c for c in ["cnpj", "nome_cliente", "data_base", "data_hora_cadastro", "dt_conta_criada", "pendencias"] if c in base.columns]
    if base_info_cols:
        base_info = base[base_info_cols].copy().drop_duplicates(subset=["cnpj"], keep="last")
        out = out.merge(base_info, on="cnpj", how="left", suffixes=("", "_base"))
        for col in ["nome_cliente", "data_base", "data_hora_cadastro", "dt_conta_criada", "pendencias"]:
            base_col = f"{col}_base"
            if base_col in out.columns:
                out[col] = out[col].replace("", pd.NA).fillna(out[base_col])
                out = out.drop(columns=[base_col])

    if isinstance(oco, pd.DataFrame) and not oco.empty and "cnpj" in oco.columns:
        op_cols = [c for c in ["cnpj", "operador", "data_acao"] if c in oco.columns]
        op_info = oco[op_cols].copy().drop_duplicates(subset=["cnpj"], keep="last")
        out = out.merge(op_info, on="cnpj", how="left")
    else:
        out["operador"] = ""
        out["data_acao"] = pd.NaT

    out["status_bko"] = pd.Categorical(out["status_bko"], categories=["Ativo", "Inativo"], ordered=True)
    return out[[
        "nome_cliente", "cnpj", "operador", "data_acao", "data_base", "data_hora_cadastro",
        "status_abertura_conta", "status_bko", "status_pos_5_dias", "data_saida_bko",
        "dias_uteis_bko", "bucket_bko", "ultima_base_importada", "data_inicio_bko",
        "ultima_data_em_bko", "pendencias", "dt_conta_criada"
    ]].sort_values(["status_bko", "dias_uteis_bko", "nome_cliente"], ascending=[True, False, True], na_position="last")


def _sort_uploaded_c6_files(files) -> list:
    ranked = []
    for f in files or []:
        try:
            df = read_excel_any(f.getvalue())
            d = detect_report_day_from_df(df) or detect_report_month_from_df(df) or dt.date(1900, 1, 1)
        except Exception:
            d = dt.date(1900, 1, 1)
        ranked.append((d, f))
    ranked.sort(key=lambda x: x[0])
    return [f for _, f in ranked]


def _sort_uploaded_leads_files(files) -> list:
    ranked = []
    for f in files or []:
        try:
            df = read_excel_any(f.getvalue())
            d = _status_extract_date_base_from_col_b(df) or dt.date(1900, 1, 1)
        except Exception:
            d = dt.date(1900, 1, 1)
        ranked.append((d, f))
    ranked.sort(key=lambda x: x[0])
    return [f for _, f in ranked]


def _temp_import_files_by_keyword(keyword: str) -> list[str]:
    base_dir = os.path.join(APP_DIR, "temp_imports")
    if not os.path.isdir(base_dir):
        return []
    key = str(keyword or "").strip().lower()
    ranked = []
    for name in os.listdir(base_dir):
        full = os.path.join(base_dir, name)
        if not os.path.isfile(full):
            continue
        norm = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode("ascii").lower()
        if key not in norm:
            continue
        ranked.append(full)
    return sorted(ranked)


def _write_daily_temp_import_copy(file_name: str, raw_bytes: bytes):
    base_dir = os.path.join(APP_DIR, "temp_imports")
    os.makedirs(base_dir, exist_ok=True)
    safe_name = os.path.basename(str(file_name or "").strip())
    if not safe_name:
        return
    out_path = os.path.join(base_dir, safe_name)
    with open(out_path, "wb") as f:
        f.write(raw_bytes)


def _refresh_panel_c6_histories_from_temp_imports(saved_resumo: Optional[dict] = None):
    open_hist = safe_json_load(HIST_OPEN_DAILY, default={}) or {}
    leads_hist = safe_json_load(HIST_LEADS_DAILY, default={}) or {}
    compare_hist = safe_json_load(HIST_COMPARE_DAILY, default={}) or {}

    visao_files = _temp_import_files_by_keyword("visao")
    for path in visao_files:
        try:
            _, mtime_ns, size = _safe_file_signature(path)
            df = _read_excel_path_cached(path, mtime_ns, size)
        except Exception:
            continue
        if df is None or df.empty:
            continue

        _persist_visao_month_snapshot(df)
        try:
            # Regrava o histórico do supervisor com a lógica atual,
            # para a evolução diária não continuar presa em snapshots antigos.
            persist_supervisor_c6_daily(df)
        except Exception:
            pass
        df_panel = _panel_c6_valid_df(df)

        if COL_ABERTURA not in df.columns:
            df[COL_ABERTURA] = pd.NaT
        if COL_CASHIN_MTD not in df.columns:
            df[COL_CASHIN_MTD] = 0.0
        if COL_BR not in df.columns:
            df[COL_BR] = ""
        if COL_PIX not in df.columns:
            df[COL_PIX] = ""

        if COL_ABERTURA not in df_panel.columns:
            df_panel[COL_ABERTURA] = pd.NaT
        if COL_CASHIN_MTD not in df_panel.columns:
            df_panel[COL_CASHIN_MTD] = 0.0
        if COL_BR not in df_panel.columns:
            df_panel[COL_BR] = ""
        if COL_PIX not in df_panel.columns:
            df_panel[COL_PIX] = ""

        df[COL_ABERTURA] = to_date_series(df[COL_ABERTURA])
        df[COL_CASHIN_MTD] = pd.to_numeric(df[COL_CASHIN_MTD], errors="coerce").fillna(0.0)
        df[COL_BR] = normalize_str(df[COL_BR]).str.upper()

        df_panel[COL_ABERTURA] = to_date_series(df_panel[COL_ABERTURA])
        df_panel[COL_CASHIN_MTD] = pd.to_numeric(df_panel[COL_CASHIN_MTD], errors="coerce").fillna(0.0)
        df_panel[COL_BR] = normalize_str(df_panel[COL_BR]).str.upper()

        opened_counts = (
            df[df[COL_ABERTURA].notna()]
            .assign(_d=df[COL_ABERTURA])
            .query("_d >= @HIST_START")
            .groupby("_d")
            .size()
            .to_dict()
        )
        for d, qty in opened_counts.items():
            open_hist[fmt_date(d)] = int(qty)

        report_day = detect_report_day_from_df(df)
        report_month = detect_report_month_from_df(df)
        if report_day and report_day >= HIST_START:
            dqq = df.copy()
            dqq["_nivel"] = parse_level(dqq)
            qmask = dqq["_nivel"] >= 1
            br_tmp = normalize_str(dqq.get(COL_BR, pd.Series([""] * len(dqq), index=dqq.index))).str.upper()
            pix_total = int(dqq.get(COL_PIX, pd.Series([""] * len(dqq), index=dqq.index)).apply(_pix_clean_value).apply(_pix_is_valid).sum())
            base_receber_mes = _old_rule_receber_from_visao_df(df, all_rows=True)
            day_key = fmt_date(report_day)
            rec = compare_hist.get(day_key, {}) or {}
            rec.update({
                "mes_ref": fmt_month(report_month) if report_month else str(rec.get("mes_ref", "") or ""),
                "c6_total": int(len(df)),
                "qual_total": int(qmask.sum()),
                "qual_m0": int((qmask & (br_tmp == "M0")).sum()),
                "qual_m1": int((qmask & (br_tmp == "M1")).sum()),
                "qual_m2": int((qmask & (br_tmp == "M2")).sum()),
                "pix_total": pix_total,
                "cashin_total": float(dqq[COL_CASHIN_MTD].sum()),
                "base_receber_mes": float(base_receber_mes),
            })
            compare_hist[day_key] = rec

    leads_files = _temp_import_files_by_keyword("leads")
    for path in leads_files:
        try:
            _, mtime_ns, size = _safe_file_signature(path)
            df = _read_excel_path_cached(path, mtime_ns, size)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        if COL_LEADS_DATA not in df.columns:
            cand = [c for c in df.columns if "CADAST" in str(c).upper() and "DATA" in str(c).upper()]
            if cand:
                df[COL_LEADS_DATA] = df[cand[0]]
            elif len(df.columns) >= 13:
                df[COL_LEADS_DATA] = df.iloc[:, 12]
            else:
                df[COL_LEADS_DATA] = pd.NA
        df[COL_LEADS_DATA] = to_date_series(df[COL_LEADS_DATA])
        leads_counts = (
            df[df[COL_LEADS_DATA].notna()]
            .assign(_d=df[COL_LEADS_DATA])
            .query("_d >= @HIST_START")
            .groupby("_d")
            .size()
            .to_dict()
        )
        for d, qty in leads_counts.items():
            leads_hist[fmt_date(d)] = int(qty)

        report_day = detect_report_day_from_df(df)
        if report_day and report_day >= HIST_START:
            day_key = fmt_date(report_day)
            rec = compare_hist.get(day_key, {}) or {}
            if not rec.get("mes_ref"):
                rec["mes_ref"] = fmt_month(dt.date(report_day.year, report_day.month, 1))
            rec["leads_total"] = int(len(df))
            compare_hist[day_key] = rec

    if saved_resumo is None:
        saved_resumo = safe_json_load(HIST_RESUMO_MENSAL, default={}) or {}

    for day_key, rec in list(compare_hist.items()):
        mes_ref = str(rec.get("mes_ref", "") or "")
        if "base_receber_mes" not in rec or rec.get("base_receber_mes") in (None, ""):
            rec["base_receber_mes"] = float((saved_resumo.get(mes_ref) or {}).get("receber_mes", 0.0)) if mes_ref else 0.0
        compare_hist[day_key] = rec

    safe_json_save(HIST_OPEN_DAILY, open_hist)
    safe_json_save(HIST_LEADS_DAILY, leads_hist)
    safe_json_save(HIST_COMPARE_DAILY, compare_hist)


def _refresh_panel_c6_histories_from_current_daily(
    df_c6: Optional[pd.DataFrame] = None,
    df_leads: Optional[pd.DataFrame] = None,
    saved_resumo: Optional[dict] = None,
):
    leads_day = detect_report_day_from_df(df_leads) if df_leads is not None and not df_leads.empty else None
    visao_day = detect_report_day_from_df(df_c6) if df_c6 is not None and not df_c6.empty else None
    if (df_c6 is None or df_c6.empty) and leads_day:
        df_tmp, _ = _load_temp_import_daily_df("visao", leads_day)
        if df_tmp is not None and not df_tmp.empty:
            df_c6 = df_tmp
            visao_day = leads_day
    if (df_leads is None or df_leads.empty) and visao_day:
        df_tmp, _ = _load_temp_import_daily_df("leads", visao_day)
        if df_tmp is not None and not df_tmp.empty:
            df_leads = df_tmp
            leads_day = visao_day

    pending = {}
    if df_c6 is not None and not df_c6.empty:
        try:
            _persist_visao_month_snapshot(df_c6)
        except Exception:
            pass
        try:
            persist_supervisor_c6_daily(df_c6)
        except Exception:
            pass
        pending = _refresh_compare_pending_from_daily_c6(df_c6, pending)
    if df_leads is not None and not df_leads.empty:
        pending = _refresh_compare_pending_from_daily_leads(df_leads, pending)
    if not pending:
        return

    if saved_resumo is None:
        saved_resumo = safe_json_load(HIST_RESUMO_MENSAL, default={}) or {}

    for day_key, rec in pending.items():
        mes_ref = str(rec.get("mes_ref", "") or "")
        if "base_receber_mes" in rec:
            base_receber_mes = float(rec.get("base_receber_mes", 0.0) or 0.0)
        elif mes_ref:
            base_receber_mes = float((saved_resumo.get(mes_ref) or {}).get("receber_mes", 0.0))
        else:
            base_receber_mes = 0.0

        payload_cmp = {
            "mes_ref": mes_ref,
            "base_receber_mes": float(base_receber_mes),
        }
        for key in ["c6_total", "leads_total", "qual_total", "qual_m0", "qual_m1", "qual_m2", "pix_total"]:
            if key in rec:
                payload_cmp[key] = int(rec.get(key, 0) or 0)
        if "cashin_total" in rec:
            payload_cmp["cashin_total"] = float(rec.get("cashin_total", 0.0) or 0.0)
        compare_daily_upsert(day_key, payload_cmp)


def _status_extract_date_base_from_col_b(df: pd.DataFrame) -> Optional[dt.date]:
    if df is None or df.empty or df.shape[1] < 2:
        return None
    s = df.iloc[:, 1]
    d = pd.to_datetime(s, errors="coerce", dayfirst=True).dt.date.dropna()
    if d.empty:
        return None
    m = d.mode()
    if len(m) > 0:
        return m.iloc[0]
    return max(d)


def _load_daily_import_cache(kind: str):
    kind_key = str(kind or "").strip().lower()
    if kind_key == "visao":
        cache_path = C6_DAILY_VISAO_CACHE
    elif kind_key == "lct":
        cache_path = C6_DAILY_LCT_CACHE
    else:
        cache_path = C6_DAILY_LEADS_CACHE
    if "firebase" in st.secrets:
        payload, meta, origin = _load_cloud_daily_import_payload(cache_path, kind_key)
        if payload:
            df = _df_from_store_payload(payload)
            info = meta.get(kind_key) or {}
            return df, str(info.get("name") or ""), origin
        return None, "", ""
    if not os.path.exists(cache_path):
        return None, "", ""
    try:
        with open(cache_path, "rb") as f:
            raw = f.read()
        info = (local_json_load(C6_DAILY_IMPORT_META, default={}) or {}).get(kind_key) or {}
        file_name = str(info.get("name") or "")
        if file_name.lower().endswith(".csv"):
            sample = raw[:200_000].decode("utf-8-sig", errors="replace")
            candidates = [";", ",", "\t", "|"]
            counts = {sep: sample.count(sep) for sep in candidates}
            sep = max(counts, key=counts.get) if counts else ","
            if counts.get(sep, 0) <= 0:
                sep = ","
            df = pd.read_csv(io.BytesIO(raw), sep=sep, engine="python", on_bad_lines="skip", encoding="utf-8-sig")
        else:
            df = read_excel_any(raw)
        meta = local_json_load(C6_DAILY_IMPORT_META, default={}) or {}
        info = meta.get(kind_key) or {}
        file_name = str(info.get("name") or os.path.basename(cache_path))
        return df, file_name, "Importação diária (cache local)"
    except Exception:
        return None, "", ""


def _status_calcular_validas_14d(df: pd.DataFrame, data_base: dt.date) -> int:
    colunas_cadastro = [c for c in df.columns if "DATA_HORA_CADASTRO" in str(c).upper()]
    if not colunas_cadastro:
        colunas_cadastro = [c for c in df.columns if "CADAST" in str(c).upper() and "DATA" in str(c).upper()]
    if not colunas_cadastro:
        return 0
    cad_dt = pd.to_datetime(df[colunas_cadastro[0]], errors="coerce", dayfirst=True)
    if cad_dt.isna().all():
        return 0
    base_ts = pd.Timestamp(data_base)
    diff_days = (base_ts - cad_dt).dt.days
    mask = diff_days.notna() & (diff_days >= 0) & (diff_days <= 14)
    return int(mask.sum())


def _status_limpar_nome(status: str) -> str:
    if not isinstance(status, str):
        status = str(status)
    nome = status.strip()
    nome = nome.replace("'", "")
    nome = nome.replace('"', "")
    nome = nome.replace("`", "")
    nome = nome.replace("-", " ")
    nome = nome.replace("_", " ")
    nome = " ".join(nome.split())
    return firestore_safe_key(nome, max_len=120)


def _upsert_leads_status_from_df(df_status: pd.DataFrame, source_name: str = "", source_hash: str = "") -> Dict[str, object]:
    if df_status is None or df_status.empty:
        return {"ok": False, "reason": "empty"}
    if df_status.shape[1] < 17:
        return {"ok": False, "reason": "missing_q"}

    data_base = _status_extract_date_base_from_col_b(df_status)
    if data_base is None:
        return {"ok": False, "reason": "missing_date"}

    store = safe_json_load(LEADS_STATUS_DAILY_PATH, default={}) or {}
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

    st.info("Esta aba já é alimentada automaticamente pela importação diária do Painel C6 Empresas. Não é necessário importar novamente aqui.")
    if "c6_daily_leads_df" in st.session_state:
        nome_origem = str(st.session_state.get("c6_daily_leads_df__name", "") or "Planilha Leads — diária")
        st.caption(f"Último arquivo reaproveitado do Painel C6 Empresas: {nome_origem}")
    else:
        st.caption("Quando você importar a Planilha Leads — diária no Painel C6 Empresas, os dados aparecerão aqui automaticamente.")

    s = df_status.iloc[:, 16].astype("string").fillna("").str.strip()
    s = s[s != ""]
    if s.empty:
        status_counts = {}
    else:
        s_limpo = s.apply(_status_limpar_nome)
        status_counts = {str(k): int(v) for k, v in s_limpo.value_counts().to_dict().items()}

    payload = dict(status_counts)
    payload["_validas_14d"] = int(_status_calcular_validas_14d(df_status, data_base))

    day_key_iso = day_key_store_iso(data_base)
    store[day_key_iso] = payload
    _leads_control = safe_json_load(LEADS_CONTROL_PATH, default={}) or {}
    files_meta = _leads_control.get("files", []) or []
    imported_at = dt.datetime.now().isoformat()

    if source_name or source_hash:
        files_meta = [
            item for item in files_meta
            if not (
                item.get("day") == day_key_iso
                and item.get("name") == source_name
                and item.get("hash") == source_hash
            )
        ]
        files_meta.append({
            "name": source_name or "importacao_diaria",
            "hash": source_hash,
            "day": day_key_iso,
            "imported_at": imported_at,
            "origin": "importacao_diaria",
        })

    _leads_control["files"] = files_meta[-1000:]
    _leads_control["updated_at"] = imported_at
    _leads_control["last_processed"] = {"day": day_key_iso, "imported_at": imported_at}

    safe_json_save(LEADS_CONTROL_PATH, _leads_control)
    safe_json_save(LEADS_STATUS_DAILY_PATH, store)

    return {"ok": True, "day": day_key_iso, "validas": payload["_validas_14d"]}


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
# ✅ CONTROLE GENÉRICO (Meta) — mínimo para integrar o bloco
# =========================================================
def _normalize_files_meta_list(files) -> List[dict]:
    out = []
    if not isinstance(files, list):
        return out
    for f in files:
        if not isinstance(f, dict):
            continue
        name = f.get("name")
        h = f.get("hash")
        if not name or not h:
            continue
        out.append({"name": str(name), "hash": str(h), "size": f.get("size")})
    return out

def get_file_control(control_path: str) -> dict:
    c = safe_json_load(control_path, default={}) or {}
    if not isinstance(c, dict):
        c = {}
    c["files"] = _normalize_files_meta_list(c.get("files", []))
    c["file_hashes"] = [f.get("hash") for f in c["files"] if isinstance(f, dict) and f.get("hash")]
    return c

def save_file_control(control_path: str, control: dict):
    if not isinstance(control, dict):
        control = {}
    control["files"] = _normalize_files_meta_list(control.get("files", []))
    control["file_hashes"] = [f.get("hash") for f in control["files"] if isinstance(f, dict) and f.get("hash")]
    safe_json_save(control_path, control)


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
    s = df.get(COL_PIX, pd.Series([""] * len(df))).apply(
        lambda x: _pix_clean_value(x) if _pix_is_valid(x) else ""
    )
    has_pix = s.apply(_pix_is_valid)

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
    current = base.get(day_key, {}) if isinstance(base, dict) else {}
    merged = dict(current or {})
    merged.update(payload or {})
    base[day_key] = merged
    safe_json_save(HIST_COMPARE_DAILY, base)


def compare_daily_raw_df() -> pd.DataFrame:
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

    return pd.DataFrame(rows).sort_values("_date", ascending=True).reset_index(drop=True)


def compare_daily_df() -> pd.DataFrame:
    df = compare_daily_raw_df()
    if df.empty:
        return pd.DataFrame()

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
        "Base (A receber no mês)", "Δ Base (A receber no mês)",
        "Qualificadas M0", "Δ Qualificadas M0",
        "Qualificadas M1", "Δ Qualificadas M1",
        "Qualificadas M2", "Δ Qualificadas M2",
        "Chaves Pix total", "Δ Chaves Pix total",
        "Saldo total (VL_CASH_IN_MTD)", "Δ Saldo total (VL_CASH_IN_MTD)"
    ]].rename(columns={"_mes_ref": "Mês ref (remuneração)"})

    return df


def compare_same_day_across_months_df(day_of_month: int) -> pd.DataFrame:
    df = compare_daily_raw_df()
    if df.empty:
        return pd.DataFrame()

    df = df[df["_date"].apply(lambda d: int(d.day) == int(day_of_month))].copy()
    if df.empty:
        return pd.DataFrame()

    df = df.sort_values("_date", ascending=True).reset_index(drop=True)

    for col in [
        "Contas (C6) total",
        "Leads total",
        "Qualificadas total",
        "Qualificadas M0",
        "Qualificadas M1",
        "Qualificadas M2",
        "Chaves Pix total",
        "Saldo total (VL_CASH_IN_MTD)",
        "Base (A receber no mÃªs)",
    ]:
        df[f"Î” mÃªs anterior {col}"] = df[col].diff().fillna(0)

    df = df.sort_values("_date", ascending=False).reset_index(drop=True)

    df["Saldo total (VL_CASH_IN_MTD)"] = df["Saldo total (VL_CASH_IN_MTD)"].apply(br_money)
    df["Î” mÃªs anterior Saldo total (VL_CASH_IN_MTD)"] = df["Î” mÃªs anterior Saldo total (VL_CASH_IN_MTD)"].apply(br_money)
    df["Base (A receber no mÃªs)"] = df["Base (A receber no mÃªs)"].apply(br_money)
    df["Î” mÃªs anterior Base (A receber no mÃªs)"] = df["Î” mÃªs anterior Base (A receber no mÃªs)"].apply(br_money)

    for c in [
        "Contas (C6) total",
        "Leads total",
        "Qualificadas total",
        "Qualificadas M0",
        "Qualificadas M1",
        "Qualificadas M2",
        "Chaves Pix total",
        "Î” mÃªs anterior Contas (C6) total",
        "Î” mÃªs anterior Leads total",
        "Î” mÃªs anterior Qualificadas total",
        "Î” mÃªs anterior Qualificadas M0",
        "Î” mÃªs anterior Qualificadas M1",
        "Î” mÃªs anterior Qualificadas M2",
        "Î” mÃªs anterior Chaves Pix total",
    ]:
        df[c] = df[c].apply(br_int)

    df["Dia comparado"] = br_int(int(day_of_month))
    df = df[[
        "Dia comparado", "Data base", "_mes_ref",
        "Contas (C6) total", "Î” mÃªs anterior Contas (C6) total",
        "Leads total", "Î” mÃªs anterior Leads total",
        "Qualificadas total", "Î” mÃªs anterior Qualificadas total",
        "Qualificadas M0", "Î” mÃªs anterior Qualificadas M0",
        "Qualificadas M1", "Î” mÃªs anterior Qualificadas M1",
        "Qualificadas M2", "Î” mÃªs anterior Qualificadas M2",
        "Chaves Pix total", "Î” mÃªs anterior Chaves Pix total",
        "Saldo total (VL_CASH_IN_MTD)", "Î” mÃªs anterior Saldo total (VL_CASH_IN_MTD)",
        "Base (A receber no mÃªs)", "Î” mÃªs anterior Base (A receber no mÃªs)"
    ]].rename(columns={"_mes_ref": "MÃªs ref (remuneraÃ§Ã£o)"})

    return df


def compare_same_day_across_months_df(day_of_month: int) -> pd.DataFrame:
    df = compare_daily_raw_df()
    if df.empty:
        return pd.DataFrame()

    df = df[df["_date"].apply(lambda d: int(d.day) == int(day_of_month))].copy()
    if df.empty:
        return pd.DataFrame()

    df = df.sort_values("_date", ascending=True).reset_index(drop=True)
    saldo_col = "Saldo total (VL_CASH_IN_MTD)"
    base_col = next((c for c in df.columns if c.startswith("Base (A receber")), None)
    if not base_col:
        return pd.DataFrame()

    calc_cols = [
        "Contas (C6) total",
        "Leads total",
        "Qualificadas total",
        "Qualificadas M0",
        "Qualificadas M1",
        "Qualificadas M2",
        "Chaves Pix total",
        saldo_col,
        base_col,
    ]
    for col in calc_cols:
        df[f"Delta mes anterior {col}"] = df[col].diff().fillna(0)

    df = df.sort_values("_date", ascending=False).reset_index(drop=True)
    df[saldo_col] = df[saldo_col].apply(br_money)
    df[f"Delta mes anterior {saldo_col}"] = df[f"Delta mes anterior {saldo_col}"].apply(br_money)
    df[base_col] = df[base_col].apply(br_money)
    df[f"Delta mes anterior {base_col}"] = df[f"Delta mes anterior {base_col}"].apply(br_money)

    for c in [
        "Contas (C6) total",
        "Leads total",
        "Qualificadas total",
        "Qualificadas M0",
        "Qualificadas M1",
        "Qualificadas M2",
        "Chaves Pix total",
        "Delta mes anterior Contas (C6) total",
        "Delta mes anterior Leads total",
        "Delta mes anterior Qualificadas total",
        "Delta mes anterior Qualificadas M0",
        "Delta mes anterior Qualificadas M1",
        "Delta mes anterior Qualificadas M2",
        "Delta mes anterior Chaves Pix total",
    ]:
        df[c] = df[c].apply(br_int)

    df["Dia comparado"] = br_int(int(day_of_month))
    df = df[[
        "Dia comparado", "Data base", "_mes_ref",
        "Contas (C6) total", "Delta mes anterior Contas (C6) total",
        "Leads total", "Delta mes anterior Leads total",
        "Qualificadas total", "Delta mes anterior Qualificadas total",
        base_col, f"Delta mes anterior {base_col}",
        "Qualificadas M0", "Delta mes anterior Qualificadas M0",
        "Qualificadas M1", "Delta mes anterior Qualificadas M1",
        "Qualificadas M2", "Delta mes anterior Qualificadas M2",
        "Chaves Pix total", "Delta mes anterior Chaves Pix total",
        saldo_col, f"Delta mes anterior {saldo_col}"
    ]].rename(columns={
        "_mes_ref": "Mes ref (remuneracao)",
        base_col: "Base (A receber no mes)",
        f"Delta mes anterior {base_col}": "Delta mes anterior Base (A receber no mes)",
        f"Delta mes anterior {saldo_col}": "Delta mes anterior Saldo total (VL_CASH_IN_MTD)",
    })

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

    if mes_rel < HIST_START:
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


def _normalize_cnpj_text(v) -> str:
    return re.sub(r"\D", "", "" if v is None or pd.isna(v) else str(v))


def _nova_acc_factor(qtd_qualificadas: int) -> float:
    for min_q, factor in ACELERADORES_NOVA:
        if qtd_qualificadas >= min_q:
            return float(factor)
    return 1.0


def _nova_cartilha_fator_por_qualificadas(qtd: int) -> float:
    """Alinhado à coluna MULTIPLICADOR_NOVA_CARTILHA (=1) quando aceleradores estão desligados."""
    if not NOVA_CARTILHA_USAR_ACELERADORES:
        return 1.0
    return float(_nova_acc_factor(int(qtd)))


def _nova_paid_max_from_store() -> Dict[str, float]:
    """Teto já atingido na cartilha nova (por CNPJ). Chaves normalizadas só com dígitos."""
    raw = safe_json_load(HIST_NOVA_PAGO_POR_CNPJ, default={}) or {}
    out: Dict[str, float] = {}
    for k, v in raw.items():
        nk = _normalize_cnpj_text(k)
        if not nk:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        out[nk] = max(float(out.get(nk, 0.0)), fv)
    return out


def _nova_paid_max_from_store_before(target_month: str) -> Dict[str, float]:
    """Teto salvo somente de meses anteriores ao alvo; evita maio abater contra maio."""
    raw = safe_json_load(HIST_NOVA_PAGO_POR_CNPJ, default={}) or {}
    out: Dict[str, float] = {}
    target_key = month_key_str(target_month)
    for k, v in raw.items():
        nk = _normalize_cnpj_text(k)
        if not nk:
            continue
        month_txt = ""
        value = 0.0
        if isinstance(v, dict):
            month_txt = str(v.get("month") or v.get("mes") or "").strip()
            try:
                value = float(v.get("max_paid", v.get("valor", 0.0)) or 0.0)
            except (TypeError, ValueError):
                continue
        else:
            # Formato antigo não tinha mês; não é seguro usar, pois pode ter sido gravado com o próprio maio.
            continue
        if month_txt and month_key_str(month_txt) < target_key:
            out[nk] = max(float(out.get(nk, 0.0)), value)
    return out


def _nova_cartilha_paid_max_start(months_here: List[str]) -> Dict[str, dict]:
    """Se só existir snapshot a partir de maio (senão abril no histórico), repõe o teto já acumulado em disco."""
    if not months_here:
        return {}
    cart_ini = sorted(CARTILHA_NOVA_MESES, key=month_key_str)[0]
    first_here = sorted(months_here, key=month_key_str)[0]
    if month_key_str(first_here) > month_key_str(cart_ini):
        return {
            k: {"max_paid": v, "month": _shift_month_key(first_here, -1)}
            for k, v in _nova_paid_max_from_store_before(first_here).items()
        }
    return {}


def _nova_paid_value(paid_max: dict, cnpj: str, current_month: str) -> float:
    item = (paid_max or {}).get(_normalize_cnpj_text(cnpj), 0.0)
    if isinstance(item, dict):
        month_txt = str(item.get("month") or item.get("mes") or "").strip()
        if month_txt and month_key_str(month_txt) >= month_key_str(current_month):
            return 0.0
        try:
            return float(item.get("max_paid", item.get("valor", 0.0)) or 0.0)
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(item or 0.0)
    except (TypeError, ValueError):
        return 0.0


@lru_cache(maxsize=24)
def _nova_paid_ref_for_month(month_key: str) -> Dict[str, float]:
    try:
        ref = safe_json_load(HIST_NOVA_PAID_REF, default={}) or {}
        ref_month = ref.get(month_key, {}) or {}
        return {
            _normalize_cnpj_text(k): float(v or 0.0)
            for k, v in ref_month.items()
            if _normalize_cnpj_text(k)
        }
    except Exception:
        return {}


def _new_cartilha_full_amount_from_row(row: dict) -> float:
    if str((row or {}).get("mes_ref_comiss", "") or "").strip().upper() not in {"M0", "M1", "M2"}:
        return 0.0
    fator = float(_nova_cartilha_fator_por_qualificadas(0))
    return max(
        _nova_cashin_amount(float((row or {}).get("cash_in_valor", 0.0) or 0.0), fator),
        _nova_spending_amount(float((row or {}).get("spending_total_mtd", 0.0) or 0.0), fator),
        _nova_tpv_amount(_nova_tpv_for_cartilha(row or {}), fator),
    )


def _old_cartilha_full_by_month(mkey: str) -> Dict[str, float]:
    levels = _visao_month_old_rule_levels(mkey)
    if not levels:
        return {}
    _, precos = faixa_por_qtd(len(levels)) if mkey != "12/2025" else (FAIXAS[-1][1], FAIXAS[-1][2])
    return {
        _normalize_cnpj_text(cnpj): float(precos.get(int(lvl), 0.0))
        for cnpj, lvl in levels.items()
        if _normalize_cnpj_text(cnpj) and int(lvl) >= 1
    }


def _new_cartilha_full_by_month(mkey: str) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for cnpj, row in _visao_month_valid_rows(mkey).items():
        full = _new_cartilha_full_amount_from_row(row)
        if full > 0:
            out[_normalize_cnpj_text(cnpj)] = float(full)
    return out


@lru_cache(maxsize=24)
def _winner_paid_before_month(current_month: str) -> Dict[str, float]:
    """Memória única: o banco paga a maior cartilha em abr/mai/jun e esse valor vira já pago por CNPJ."""
    paid: Dict[str, float] = {}
    if month_key_str(current_month) > month_key_str("04/2026"):
        paid.update(_old_paid_max_before("04/2026") or {})

    for mkey in sorted(CARTILHA_NOVA_MESES, key=month_key_str):
        if month_key_str(mkey) >= month_key_str(current_month):
            break

        old_full = _old_cartilha_full_by_month(mkey)
        new_full = _new_cartilha_full_by_month(mkey)
        old_receive = sum(max(0.0, float(v) - float(paid.get(k, 0.0))) for k, v in old_full.items())
        new_receive = sum(max(0.0, float(v) - float(paid.get(k, 0.0))) for k, v in new_full.items())
        winner_full = new_full if new_receive >= old_receive else old_full

        for cnpj, full in winner_full.items():
            if full > 0:
                paid[cnpj] = max(float(paid.get(cnpj, 0.0)), float(full))
    return paid


def _nova_prior_paid_value(paid_max: dict, cnpj: str, current_month: str) -> float:
    """Abate o valor já pago pela cartilha vencedora dos meses anteriores."""
    nk = _normalize_cnpj_text(cnpj)
    winner_prev = _winner_paid_before_month(current_month)
    ref_month = _nova_paid_ref_for_month(current_month)
    if nk in winner_prev:
        return max(float(winner_prev.get(nk, 0.0) or 0.0), float(ref_month.get(nk, 0.0) or 0.0))
    if nk in ref_month:
        return float(ref_month.get(nk, 0.0) or 0.0)
    nova_prev = _nova_paid_value(paid_max, nk, current_month)
    old_prev = 0.0
    try:
        old_prev = float((_old_paid_max_before(current_month) or {}).get(nk, 0.0) or 0.0)
    except Exception:
        old_prev = 0.0
    return max(nova_prev, old_prev)


@lru_cache(maxsize=24)
def _old_paid_ref_for_month(month_key: str) -> Dict[str, float]:
    try:
        ref = safe_json_load(HIST_OLD_PAID_REF, default={}) or {}
        ref_month = ref.get(month_key, {}) or {}
        return {
            _normalize_cnpj_text(k): float(v or 0.0)
            for k, v in ref_month.items()
            if _normalize_cnpj_text(k)
        }
    except Exception:
        return {}


def _old_prior_paid_value(old_paid_before: dict, cnpj: str, current_month: str) -> float:
    """Abate o valor já pago pela cartilha vencedora dos meses anteriores."""
    nk = _normalize_cnpj_text(cnpj)
    winner_prev = _winner_paid_before_month(current_month)
    ref_month = _old_paid_ref_for_month(current_month)
    if nk in winner_prev:
        return max(float(winner_prev.get(nk, 0.0) or 0.0), float(ref_month.get(nk, 0.0) or 0.0))
    if nk in ref_month:
        return float(ref_month.get(nk, 0.0) or 0.0)
    old_prev = 0.0
    try:
        old_prev = float((old_paid_before or {}).get(nk, 0.0) or 0.0)
    except Exception:
        old_prev = 0.0
    nova_prev = 0.0
    try:
        nova_prev = float((_nova_paid_max_from_store_before(current_month) or {}).get(nk, 0.0) or 0.0)
    except Exception:
        nova_prev = 0.0
    return max(old_prev, nova_prev)


def _nova_paid_update(paid_max: dict, cnpj: str, month_key: str, amount: float):
    nk = _normalize_cnpj_text(cnpj)
    if not nk:
        return
    current = paid_max.get(nk, {})
    prev_val = 0.0
    prev_month = ""
    if isinstance(current, dict):
        prev_month = str(current.get("month") or current.get("mes") or "").strip()
        try:
            prev_val = float(current.get("max_paid", current.get("valor", 0.0)) or 0.0)
        except (TypeError, ValueError):
            prev_val = 0.0
    else:
        try:
            prev_val = float(current or 0.0)
        except (TypeError, ValueError):
            prev_val = 0.0
    new_val = max(prev_val, float(amount or 0.0))
    keep_month = month_key
    if prev_month and prev_val >= new_val:
        keep_month = prev_month
    paid_max[nk] = {"max_paid": new_val, "month": keep_month}


def _nova_cashin_amount(cashin_value: float, factor: float = 1.0) -> float:
    v = float(cashin_value or 0.0)
    f = float(factor)
    if v >= 45000:
        base = 750.0
    elif v >= 20000:
        base = 600.0
    elif v >= 10000:
        base = 400.0
    elif v >= 5000:
        base = 250.0
    else:
        base = 0.0
    return base * f


def _nova_spending_amount(spending_value: float, factor: float) -> float:
    v = float(spending_value or 0.0)
    if v >= 12000:
        base = 1400.0
    elif v >= 8000:
        base = 1100.0
    elif v >= 6000:
        base = 800.0
    elif v >= 4000:
        base = 500.0
    else:
        base = 0.0
    return float(base) * float(factor)


def _nova_tpv_amount(tpv_value: float, factor: float) -> float:
    v = float(tpv_value or 0.0)
    if v >= 30000:
        base = 2400.0
    elif v >= 15000:
        base = 1600.0
    elif v >= 8000:
        base = 1000.0
    elif v >= 3000:
        base = 600.0
    else:
        base = 0.0
    return base * float(factor)


def _nova_tpv_for_stage(row: dict) -> float:
    stage = str(row.get("mes_ref_comiss", "")).strip().upper()
    if stage == "M0":
        return float(row.get("tpv_m0", 0.0) or 0.0)
    if stage == "M1":
        return float(row.get("tpv_m1", 0.0) or 0.0)
    if stage == "M2":
        return float(row.get("tpv_m2", 0.0) or 0.0)
    return 0.0


def _nova_tpv_for_cartilha(row: dict) -> float:
    if str((row or {}).get("mes_ref_comiss", "") or "").strip().upper() not in {"M0", "M1", "M2"}:
        return 0.0
    return float(row.get("tpv_m0_valor", row.get("tpv_m0", 0.0)) or 0.0)


def _num_from_row(row, col: str, default: float = 0.0) -> float:
    try:
        return float(pd.to_numeric(pd.Series([row.get(col)]), errors="coerce").fillna(default).iloc[0])
    except Exception:
        return float(default)


def _nova_bank_cartilha_values(row: dict) -> Optional[Tuple[float, float]]:
    if not bool(row.get("nova_cartilha_bank_present", False)):
        return None
    full = float(row.get("nova_cartilha_apuracao", 0.0) or 0.0) * float(row.get("nova_cartilha_multiplicador", 1.0) or 1.0)
    receive = float(row.get("nova_cartilha_previsao", 0.0) or 0.0)
    return max(0.0, full), max(0.0, receive)


def _nova_month_uses_bank_values(valid_rows: list) -> bool:
    return any(bool((row or {}).get("nova_cartilha_bank_present", False)) for _, row in valid_rows)


def _old_bank_cartilha_values(row: dict) -> Optional[Tuple[float, float, float]]:
    if not bool(row.get("old_cartilha_bank_present", False)):
        return None
    full = float(row.get("old_cartilha_apuracao", 0.0) or 0.0) * float(row.get("old_cartilha_multiplicador", 1.0) or 1.0)
    paid = float(row.get("old_cartilha_ja_pago", 0.0) or 0.0)
    receive = float(row.get("old_cartilha_previsao", 0.0) or 0.0)
    return max(0.0, full), max(0.0, paid), max(0.0, receive)


def _month_range(mkey: str) -> Tuple[Optional[dt.date], Optional[dt.date]]:
    try:
        mm, aa = mkey.split("/")
        start = dt.date(int(aa), int(mm), 1)
        if int(mm) == 12:
            end = dt.date(int(aa) + 1, 1, 1) - dt.timedelta(days=1)
        else:
            end = dt.date(int(aa), int(mm) + 1, 1) - dt.timedelta(days=1)
        return start, end
    except Exception:
        return None, None


def _campaign_bucket_months(mkey: str) -> List[str]:
    if mkey == "04/2026":
        return ["02/2026", "03/2026", "04/2026"]
    if mkey == "05/2026":
        return ["03/2026", "04/2026", "05/2026"]
    if mkey == "06/2026":
        return ["04/2026", "05/2026", "06/2026"]
    return []


def _persist_visao_month_snapshot(df_c6: pd.DataFrame):
    mes_rel = detect_report_month_from_df(df_c6)
    if mes_rel is None:
        return

    mkey = fmt_month(mes_rel)
    store = local_json_load(HIST_VISAO_MENSAL, default={}) or {}
    df = df_c6.copy()

    if COL_CNPJ not in df.columns:
        return

    rows = []
    for _, row in df.iterrows():
        cnpj = _normalize_cnpj_text(row.get(COL_CNPJ))
        if not cnpj:
            continue
        rows.append({
            "cnpj": cnpj,
            "nome_cliente": str(row.get("NOME_CLIENTE", "") or ""),
            "tipo_pessoa": str(row.get("TIPO_PESSOA", "") or "").strip().upper(),
            "status_cc": str(row.get(COL_STATUS, "") or "").strip().upper(),
            "dt_conta_criada": fmt_date(pd.to_datetime(row.get(COL_ABERTURA), errors="coerce")),
            "dt_fundacao_empresa": fmt_date(pd.to_datetime(row.get(COL_FUNDACAO), errors="coerce")),
            "mes_ref_comiss": str(row.get(COL_BR, "") or "").strip().upper(),
            "fl_qualificado_comiss": int(pd.to_numeric(pd.Series([row.get(COL_BY)]), errors="coerce").fillna(0).iloc[0]),
            "faixa_cash_in": int(pd.to_numeric(pd.Series([row.get("FAIXA_CASH_IN")]), errors="coerce").fillna(0).iloc[0]),
            "faixa_spending": int(pd.to_numeric(pd.Series([row.get("FAIXA_SPENDING")]), errors="coerce").fillna(0).iloc[0]),
            "cash_in_valor": float(pd.to_numeric(pd.Series([row.get("VL_CASH_IN_MTD")]), errors="coerce").fillna(0.0).iloc[0]),
            "spending_total_mtd": float(pd.to_numeric(pd.Series([row.get("VL_SPENDING_TOTAL_MTD")]), errors="coerce").fillna(0.0).iloc[0]),
            "criterios_atingidos_comiss": str(row.get(COL_CRIT, "") or ""),
            "chaves_pix_forte": str(row.get(COL_PIX, "") or "").strip().upper(),
            "status_pagamento_fatura": str(row.get("STATUS_PAGAMENTO_FATURA", "") or "").strip(),
            "tpv_m0": float(pd.to_numeric(pd.Series([row.get("TPV_M0")]), errors="coerce").fillna(0.0).iloc[0]),
            "tpv_m1": float(pd.to_numeric(pd.Series([row.get("TPV_M1")]), errors="coerce").fillna(0.0).iloc[0]),
            "tpv_m2": float(pd.to_numeric(pd.Series([row.get("TPV_M2")]), errors="coerce").fillna(0.0).iloc[0]),
            "tpv_m0_valor": float(pd.to_numeric(pd.Series([row.get("TPV_M0")]), errors="coerce").fillna(0.0).iloc[0]),
            "nova_cartilha_bank_present": any(c in row.index for c in ["APURACAO_COMISS_NOVA_CARTILHA", "MULTIPLICADOR_NOVA_CARTILHA", "PREVISAO_COMISS_NOVA_CARTILHA"]),
            "nova_cartilha_apuracao": _num_from_row(row, "APURACAO_COMISS_NOVA_CARTILHA"),
            "nova_cartilha_multiplicador": _num_from_row(row, "MULTIPLICADOR_NOVA_CARTILHA", 1.0),
            "nova_cartilha_previsao": _num_from_row(row, "PREVISAO_COMISS_NOVA_CARTILHA"),
            "old_cartilha_bank_present": any(c in row.index for c in ["APURACAO_COMISS", "MULTIPLICADOR", "JA_PAGO_COMISS", "PREVISAO_COMISS"]),
            "old_cartilha_apuracao": _num_from_row(row, "APURACAO_COMISS"),
            "old_cartilha_multiplicador": _num_from_row(row, "MULTIPLICADOR", 1.0),
            "old_cartilha_ja_pago": _num_from_row(row, "JA_PAGO_COMISS"),
            "old_cartilha_previsao": _num_from_row(row, "PREVISAO_COMISS"),
            "dt_aprovacao_pay": fmt_date(pd.to_datetime(row.get("DT_APROVACAO_PAY"), errors="coerce")),
            "dt_install_maq": fmt_date(pd.to_datetime(row.get("DT_INSTALL_MAQ"), errors="coerce")),
            "dt_ativacao_pay": fmt_date(pd.to_datetime(row.get("DT_ATIVACAO_PAY"), errors="coerce")),
            "c6pay_ativa_30": int(pd.to_numeric(pd.Series([row.get("C6PAY_ATIVA_30")]), errors="coerce").fillna(0).iloc[0]),
            "dt_entrega_cartao": fmt_date(pd.to_datetime(row.get("DT_ENTREGA_CARTAO"), errors="coerce")),
            "dt_ativ_cartao_cred": fmt_date(pd.to_datetime(row.get("DT_ATIV_CARTAO_CRED"), errors="coerce")),
            "banco_domicilio": str(row.get(COL_DOMICILIO, "") or "").strip(),
            "wallet": _wallet_raw_from_row(row),
            "telefone": str(row.get("TELEFONE", "") or "").strip(),
            "telefone_master": str(row.get("TELEFONE_MASTER", "") or "").strip(),
            "data_base": fmt_date(pd.to_datetime(row.get(COL_DATA_BASE), errors="coerce")),
        })

    by_cnpj = {}
    for item in rows:
        by_cnpj[item["cnpj"]] = item
    store[mkey] = by_cnpj
    local_json_save(HIST_VISAO_MENSAL, store)


def _load_visao_month_snapshot() -> Dict[str, Dict[str, dict]]:
    return local_json_load(HIST_VISAO_MENSAL, default={}) or {}


def _visao_month_rows_from_df(df_c6: Optional[pd.DataFrame]) -> Dict[str, dict]:
    if df_c6 is None or df_c6.empty or COL_CNPJ not in df_c6.columns:
        return {}

    rows: Dict[str, dict] = {}
    for _, row in df_c6.iterrows():
        cnpj = _normalize_cnpj_text(row.get(COL_CNPJ))
        if not cnpj:
            continue
        rows[cnpj] = {
            "cnpj": cnpj,
            "nome_cliente": str(row.get("NOME_CLIENTE", "") or ""),
            "tipo_pessoa": str(row.get("TIPO_PESSOA", "") or "").strip().upper(),
            "status_cc": str(row.get(COL_STATUS, "") or "").strip().upper(),
            "dt_conta_criada": fmt_date(pd.to_datetime(row.get(COL_ABERTURA), errors="coerce")),
            "dt_fundacao_empresa": fmt_date(pd.to_datetime(row.get(COL_FUNDACAO), errors="coerce")),
            "mes_ref_comiss": str(row.get(COL_BR, "") or "").strip().upper(),
            "fl_qualificado_comiss": int(pd.to_numeric(pd.Series([row.get(COL_BY)]), errors="coerce").fillna(0).iloc[0]),
            "faixa_cash_in": int(pd.to_numeric(pd.Series([row.get("FAIXA_CASH_IN")]), errors="coerce").fillna(0).iloc[0]),
            "faixa_spending": int(pd.to_numeric(pd.Series([row.get("FAIXA_SPENDING")]), errors="coerce").fillna(0).iloc[0]),
            "cash_in_valor": float(pd.to_numeric(pd.Series([row.get("VL_CASH_IN_MTD")]), errors="coerce").fillna(0.0).iloc[0]),
            "spending_total_mtd": float(pd.to_numeric(pd.Series([row.get("VL_SPENDING_TOTAL_MTD")]), errors="coerce").fillna(0.0).iloc[0]),
            "criterios_atingidos_comiss": str(row.get(COL_CRIT, "") or ""),
            "chaves_pix_forte": str(row.get(COL_PIX, "") or "").strip().upper(),
            "status_pagamento_fatura": str(row.get("STATUS_PAGAMENTO_FATURA", "") or "").strip(),
            "tpv_m0": float(pd.to_numeric(pd.Series([row.get("TPV_M0")]), errors="coerce").fillna(0.0).iloc[0]),
            "tpv_m1": float(pd.to_numeric(pd.Series([row.get("TPV_M1")]), errors="coerce").fillna(0.0).iloc[0]),
            "tpv_m2": float(pd.to_numeric(pd.Series([row.get("TPV_M2")]), errors="coerce").fillna(0.0).iloc[0]),
            "tpv_m0_valor": float(pd.to_numeric(pd.Series([row.get("TPV_M0")]), errors="coerce").fillna(0.0).iloc[0]),
            "nova_cartilha_bank_present": any(c in row.index for c in ["APURACAO_COMISS_NOVA_CARTILHA", "MULTIPLICADOR_NOVA_CARTILHA", "PREVISAO_COMISS_NOVA_CARTILHA"]),
            "nova_cartilha_apuracao": _num_from_row(row, "APURACAO_COMISS_NOVA_CARTILHA"),
            "nova_cartilha_multiplicador": _num_from_row(row, "MULTIPLICADOR_NOVA_CARTILHA", 1.0),
            "nova_cartilha_previsao": _num_from_row(row, "PREVISAO_COMISS_NOVA_CARTILHA"),
            "old_cartilha_bank_present": any(c in row.index for c in ["APURACAO_COMISS", "MULTIPLICADOR", "JA_PAGO_COMISS", "PREVISAO_COMISS"]),
            "old_cartilha_apuracao": _num_from_row(row, "APURACAO_COMISS"),
            "old_cartilha_multiplicador": _num_from_row(row, "MULTIPLICADOR", 1.0),
            "old_cartilha_ja_pago": _num_from_row(row, "JA_PAGO_COMISS"),
            "old_cartilha_previsao": _num_from_row(row, "PREVISAO_COMISS"),
            "dt_aprovacao_pay": fmt_date(pd.to_datetime(row.get("DT_APROVACAO_PAY"), errors="coerce")),
            "dt_install_maq": fmt_date(pd.to_datetime(row.get("DT_INSTALL_MAQ"), errors="coerce")),
            "dt_ativacao_pay": fmt_date(pd.to_datetime(row.get("DT_ATIVACAO_PAY"), errors="coerce")),
            "c6pay_ativa_30": int(pd.to_numeric(pd.Series([row.get("C6PAY_ATIVA_30")]), errors="coerce").fillna(0).iloc[0]),
            "dt_entrega_cartao": fmt_date(pd.to_datetime(row.get("DT_ENTREGA_CARTAO"), errors="coerce")),
            "dt_ativ_cartao_cred": fmt_date(pd.to_datetime(row.get("DT_ATIV_CARTAO_CRED"), errors="coerce")),
            "banco_domicilio": str(row.get(COL_DOMICILIO, "") or "").strip(),
            "wallet": _wallet_raw_from_row(row),
            "telefone": str(row.get("TELEFONE", "") or "").strip(),
            "telefone_master": str(row.get("TELEFONE_MASTER", "") or "").strip(),
            "data_base": fmt_date(pd.to_datetime(row.get(COL_DATA_BASE), errors="coerce")),
        }
    return rows


def _cached_visao_month_rows() -> Tuple[str, Dict[str, dict]]:
    df_cached, _, _ = _load_daily_import_cache("visao")
    if df_cached is None or df_cached.empty:
        return "", {}
    mes_rel = detect_report_month_from_df(df_cached)
    if mes_rel is None:
        return "", {}
    return fmt_month(mes_rel), _visao_month_rows_from_df(df_cached)


def _available_visao_month_keys() -> List[str]:
    _ensure_cartilha_month_snapshots_from_temp_imports()
    store = _load_visao_month_snapshot()
    months = set(store.keys())
    cached_mkey, cached_rows = _cached_visao_month_rows()
    if cached_mkey and cached_rows:
        months.add(cached_mkey)
    return sorted(months, key=month_key_str)


def _ensure_cartilha_month_snapshots_from_temp_imports():
    store = _load_visao_month_snapshot()
    present = set(store.keys())
    if all(m in present for m in CARTILHA_NOVA_MESES if month_key_str(m) <= month_key_str("05/2026")):
        return
    best_by_month: Dict[str, Tuple[dt.date, str, int, int]] = {}
    for path in _temp_import_files_by_keyword("visao"):
        try:
            _, mtime_ns, size = _safe_file_signature(path)
            df = _read_excel_path_cached(path, mtime_ns, size)
            mes_rel = detect_report_month_from_df(df)
            day_rel = detect_report_day_from_df(df) or mes_rel
        except Exception:
            continue
        if mes_rel is None:
            continue
        mkey = fmt_month(mes_rel)
        if mkey in CARTILHA_NOVA_MESES:
            current = best_by_month.get(mkey)
            rank_day = day_rel or dt.date(1900, 1, 1)
            rank = (rank_day, path, mtime_ns, size)
            if current is None or rank_day > current[0] or (rank_day == current[0] and os.path.basename(path) > os.path.basename(current[1])):
                best_by_month[mkey] = rank
    for mkey, (_, path, mtime_ns, size) in best_by_month.items():
        try:
            df = _read_excel_path_cached(path, mtime_ns, size)
            _persist_visao_month_snapshot(df)
            present.add(mkey)
        except Exception:
            pass


def _visao_month_rows(mkey: str) -> Dict[str, dict]:
    cached_mkey, cached_rows = _cached_visao_month_rows()
    stored_rows = (_load_visao_month_snapshot().get(mkey, {}) or {})
    if cached_mkey == mkey and cached_rows:
        stored_has_bank = any(bool((row or {}).get("nova_cartilha_bank_present", False) or (row or {}).get("old_cartilha_bank_present", False)) for row in stored_rows.values()) if isinstance(stored_rows, dict) else False
        cached_has_bank = any(bool((row or {}).get("nova_cartilha_bank_present", False) or (row or {}).get("old_cartilha_bank_present", False)) for row in cached_rows.values()) if isinstance(cached_rows, dict) else False
        if stored_has_bank and not cached_has_bank:
            return stored_rows
        return cached_rows
    return stored_rows


def recompute_cartilha_nova() -> pd.DataFrame:
    months = sorted([m for m in _available_visao_month_keys() if m in CARTILHA_NOVA_MESES], key=month_key_str)

    # Cumulativo só da cartilha nova. Se abril não vier no histórico, repõe teto já salvo para maio não “zerar” o abate.
    paid_max: Dict[str, dict] = dict(_nova_cartilha_paid_max_start(months))
    resumo: Dict[str, dict] = {}
    rows = []

    for mkey in months:
        cmap = _visao_month_rows(mkey)
        valid_rows = []
        for cnpj, row in cmap.items():
            if not _c6_visao_row_eligible_pj(row):
                continue
            valid_rows.append((cnpj, row))
        use_bank_values = _nova_month_uses_bank_values(valid_rows)

        qtd_qual = 0
        detail_counts = {"cash_in": 0, "spending": 0, "c6pay": 0, "pix_cnpj": 0, "wallet": 0}

        tmp_amounts: Dict[str, dict] = {}
        fator_pref = float(_nova_cartilha_fator_por_qualificadas(0))
        for cnpj, row in valid_rows:
            bank_vals = _nova_bank_cartilha_values(row) if use_bank_values else None
            if bank_vals is not None:
                best0 = bank_vals[0]
            else:
                cash_amt0 = _nova_cashin_amount(float(row.get("cash_in_valor", 0.0) or 0.0), fator_pref)
                spending_amt0 = _nova_spending_amount(float(row.get("spending_total_mtd", 0.0) or 0.0), fator_pref)
                tpv_amt0 = _nova_tpv_amount(_nova_tpv_for_cartilha(row), fator_pref)
                best0 = max(cash_amt0, spending_amt0, tpv_amt0)
            qualified = best0 > 0
            if qualified:
                qtd_qual += 1
            tmp_amounts[cnpj] = {"row": row}

        fator_mes = float(_nova_cartilha_fator_por_qualificadas(qtd_qual))
        total_cheio = 0.0
        total_receber = 0.0
        c6pay_credenciados = 0

        for cnpj, item in tmp_amounts.items():
            row = item["row"]
            cash_amt = _nova_cashin_amount(float(row.get("cash_in_valor", 0.0) or 0.0), fator_mes)
            spending_amt = _nova_spending_amount(float(row.get("spending_total_mtd", 0.0) or 0.0), fator_mes)
            tpv_amt = _nova_tpv_amount(_nova_tpv_for_cartilha(row), fator_mes)
            best_amt = max(cash_amt, spending_amt, tpv_amt)
            bank_vals = _nova_bank_cartilha_values(row) if use_bank_values else None
            if bank_vals is not None:
                best_amt, bank_receive = bank_vals
            else:
                bank_receive = None

            if best_amt == tpv_amt and best_amt > 0:
                detail_counts["c6pay"] += 1
            elif best_amt == spending_amt and best_amt > 0:
                detail_counts["spending"] += 1
            elif best_amt == cash_amt and best_amt > 0:
                detail_counts["cash_in"] += 1

            cnpjx = _normalize_cnpj_text(cnpj)
            prev = _nova_prior_paid_value(paid_max, cnpjx, mkey)
            diff = max(0.0, best_amt - prev) if bank_receive is None else max(0.0, bank_receive)
            pix_bonus = 0.0
            if bank_receive is None and mkey == "06/2026" and best_amt > 0 and _pix_has_cnpj(row.get("chaves_pix_forte", "")):
                pix_bonus = 15.0

            diff_total = diff + pix_bonus
            best_amt_total = best_amt + pix_bonus

            total_cheio += best_amt_total
            total_receber += diff_total
            _nova_paid_update(paid_max, cnpjx, mkey, best_amt_total)

            if pix_bonus > 0:
                detail_counts["pix_cnpj"] += 1

            dt_abertura = _parse_br_date_text(row.get("dt_conta_criada"))
            dt_install = _parse_br_date_text(row.get("dt_install_maq"))
            if dt_abertura and dt_install and fmt_month(dt_abertura) == mkey:
                c6pay_credenciados += 1
            if _truthy_flag(row.get("wallet")):
                detail_counts["wallet"] += 1

        ja_pago_ref = total_cheio - total_receber
        resumo[mkey] = {
            "qualificadas": qtd_qual,
            "acelerador": fator_mes,
            "cash_in": detail_counts["cash_in"],
            "spending": detail_counts["spending"],
            "c6pay": detail_counts["c6pay"],
            "c6pay_credenciamento": c6pay_credenciados,
            "pix_cnpj": detail_counts["pix_cnpj"],
            "wallet": detail_counts["wallet"],
            "deveria_receber": total_cheio,
            "ja_pago_ref": ja_pago_ref,
            "receber_mes": total_receber,
        }
        rows.append([
            mkey, qtd_qual, fator_mes, detail_counts["cash_in"], detail_counts["spending"],
            detail_counts["c6pay"], c6pay_credenciados, detail_counts["pix_cnpj"], detail_counts["wallet"],
            total_cheio, ja_pago_ref, total_receber
        ])

    safe_json_save(HIST_NOVA_PAGO_POR_CNPJ, paid_max)
    safe_json_save(HIST_NOVA_RESUMO_MENSAL, resumo)

    return pd.DataFrame(
        rows,
        columns=[
            "Mês", "Qualificadas", "Acelerador", "Cash In", "Spending", "C6 Pay",
            "Credenciamento C6 Pay",
            "PIX CNPJ", "Wallet", "Deveria receber (cheio)", "Já pago (referência)", "A receber no mês"
        ],
    )


def _cartilha_nova_detail_by_month(target_month: str) -> pd.DataFrame:
    months = sorted([m for m in _available_visao_month_keys() if m in CARTILHA_NOVA_MESES], key=month_key_str)
    paid_max: Dict[str, dict] = dict(_nova_cartilha_paid_max_start(months))
    detail_df = pd.DataFrame()

    for mkey in months:
        cmap = _visao_month_rows(mkey)
        valid_rows = []
        for cnpj, row in cmap.items():
            if not _c6_visao_row_eligible_pj(row):
                continue
            valid_rows.append((str(cnpj), row))
        use_bank_values = _nova_month_uses_bank_values(valid_rows)

        tmp_amounts: Dict[str, dict] = {}
        qtd_qual = 0
        for cnpj, row in valid_rows:
            bank_vals = _nova_bank_cartilha_values(row) if use_bank_values else None
            cash_amt = _nova_cashin_amount(float(row.get("cash_in_valor", 0.0) or 0.0), 1.0)
            spending_amt = _nova_spending_amount(float(row.get("spending_total_mtd", 0.0) or 0.0), 1.0)
            tpv_amt = _nova_tpv_amount(_nova_tpv_for_cartilha(row), 1.0)
            best_amt = bank_vals[0] if bank_vals is not None else max(cash_amt, spending_amt, tpv_amt)
            qualified = best_amt > 0
            if qualified:
                qtd_qual += 1
            tmp_amounts[cnpj] = {
                "row": row,
                "cash_amt": cash_amt,
                "spending_amt": spending_amt,
                "tpv_amt": tpv_amt,
                "qualified": qualified,
            }

        fator_mes = float(_nova_cartilha_fator_por_qualificadas(qtd_qual))
        detail_rows = []
        current_amounts: Dict[str, float] = {}

        for cnpj, item in tmp_amounts.items():
            row = item["row"]
            cash_amt = _nova_cashin_amount(float(row.get("cash_in_valor", 0.0) or 0.0), fator_mes)
            spending_amt = _nova_spending_amount(float(row.get("spending_total_mtd", 0.0) or 0.0), fator_mes)
            tpv_amt = _nova_tpv_amount(_nova_tpv_for_cartilha(row), fator_mes)
            best_amt = max(cash_amt, spending_amt, tpv_amt)
            bank_vals = _nova_bank_cartilha_values(row) if use_bank_values else None
            if bank_vals is not None:
                best_amt, bank_receive = bank_vals
            else:
                bank_receive = None

            criterio = "Nenhum"
            if best_amt > 0:
                if best_amt == tpv_amt:
                    criterio = "C6 Pay"
                elif best_amt == spending_amt:
                    criterio = "Spending"
                elif best_amt == cash_amt:
                    criterio = "Cash In"

            cnpjx = _normalize_cnpj_text(cnpj)
            prev = _nova_prior_paid_value(paid_max, cnpjx, mkey)
            diff = max(0.0, best_amt - prev) if bank_receive is None else max(0.0, bank_receive)
            pix_bonus = 0.0
            if bank_receive is None and mkey == "06/2026" and best_amt > 0 and _pix_has_cnpj(row.get("chaves_pix_forte", "")):
                pix_bonus = 15.0

            best_amt_total = best_amt + pix_bonus
            diff_total = diff + pix_bonus
            current_amounts[cnpjx] = best_amt_total
            detail_rows.append({
                "Mês": mkey,
                "CNPJ": cnpj,
                "Nome cliente": str(row.get("nome_cliente", "") or ""),
                "Tipo pessoa": str(row.get("tipo_pessoa", "") or ""),
                "Status CC": str(row.get("status_cc", "") or ""),
                "Mês ref comissão": str(row.get("mes_ref_comiss", "") or ""),
                "Cash In (VL_CASH_IN_MTD)": float(row.get("cash_in_valor", 0.0) or 0.0),
                "Spending cartão (VL_SPENDING_TOTAL_MTD)": float(row.get("spending_total_mtd", 0.0) or 0.0),
                "TPV C6 Pay (TPV_M0)": float(_nova_tpv_for_cartilha(row) or 0.0),
                "Qualificado cartilha nova": "SIM" if best_amt > 0 else "NÃO",
                "Acelerador": fator_mes,
                "Critério vencedor novo": criterio,
                "Valor Cash In": cash_amt,
                "Valor Spending": spending_amt,
                "Valor C6 Pay": tpv_amt,
                "Bônus PIX CNPJ": pix_bonus,
                "Banco CF apuração": float(row.get("nova_cartilha_apuracao", 0.0) or 0.0),
                "Banco CG multiplicador": float(row.get("nova_cartilha_multiplicador", 1.0) or 1.0),
                "Banco CH previsão": float(row.get("nova_cartilha_previsao", 0.0) or 0.0),
                "Valor cheio novo": best_amt_total,
                "Já pago ref novo": prev,
                "A receber novo": diff_total,
            })
            _nova_paid_update(paid_max, cnpjx, mkey, best_amt_total)

        if mkey == target_month:
            detail_df = pd.DataFrame(detail_rows)
            break

    return detail_df


def _comparativo_receita_analytic_sheets(mkey: str) -> Dict[str, pd.DataFrame]:
    old_paid_before = _old_paid_max_before(mkey)
    old_levels = _visao_month_old_rule_levels(mkey)
    qtd_qual = len(old_levels)
    faixa_nome, precos = faixa_por_qtd(qtd_qual) if mkey != "12/2025" else (FAIXAS[-1][1], FAIXAS[-1][2])

    antigo_rows = []
    for cnpj, row in _visao_month_valid_rows(mkey).items():
        nivel = int(_supervisor_level(row))
        bank_old = _old_bank_cartilha_values(row)
        if bank_old is not None:
            cheio, ja_pago, receber = bank_old
        else:
            cheio = float(precos.get(nivel, 0.0)) if nivel >= 1 else 0.0
            ja_pago = _old_prior_paid_value(old_paid_before, str(cnpj), mkey)
            receber = max(0.0, cheio - ja_pago) if nivel >= 1 else 0.0
        antigo_rows.append({
            "Mês": mkey,
            "CNPJ": str(cnpj),
            "Nome cliente": str(row.get("nome_cliente", "") or ""),
            "Tipo pessoa": str(row.get("tipo_pessoa", "") or ""),
            "Status CC": str(row.get("status_cc", "") or ""),
            "Mês ref comissão": str(row.get("mes_ref_comiss", "") or ""),
            "Faixa antiga": faixa_nome,
            "Nível antigo": nivel,
            "Critérios antigos": str(row.get("criterios_atingidos_comiss", "") or ""),
            "Banco BW apuração": float(row.get("old_cartilha_apuracao", 0.0) or 0.0),
            "Banco BX multiplicador": float(row.get("old_cartilha_multiplicador", 1.0) or 1.0),
            "Banco BY já pago": float(row.get("old_cartilha_ja_pago", 0.0) or 0.0),
            "Banco BZ previsão": float(row.get("old_cartilha_previsao", 0.0) or 0.0),
            "Valor cheio antigo": cheio,
            "Já pago ref antigo": ja_pago,
            "A receber antigo": receber,
        })

    df_antigo = pd.DataFrame(antigo_rows)
    df_novo = _cartilha_nova_detail_by_month(mkey)

    if df_antigo.empty and df_novo.empty:
        return {"Regra_Antiga": pd.DataFrame(), "Cartilha_Nova": pd.DataFrame(), "Comparativo": pd.DataFrame()}

    comp = pd.merge(
        df_antigo,
        df_novo,
        on=["Mês", "CNPJ", "Nome cliente", "Tipo pessoa", "Status CC", "Mês ref comissão"],
        how="outer",
    )
    for col in ["Banco BW apuração", "Banco BX multiplicador", "Banco BY já pago", "Banco BZ previsão", "Valor cheio antigo", "Já pago ref antigo", "A receber antigo", "Valor Cash In", "Valor Spending", "Valor C6 Pay", "Bônus PIX CNPJ", "Banco CF apuração", "Banco CG multiplicador", "Banco CH previsão", "Valor cheio novo", "Já pago ref novo", "A receber novo"]:
        if col in comp.columns:
            comp[col] = pd.to_numeric(comp[col], errors="coerce").fillna(0.0)
    if "Nível antigo" in comp.columns:
        comp["Nível antigo"] = pd.to_numeric(comp["Nível antigo"], errors="coerce").fillna(0).astype(int)
    comp["Maior valor no mês"] = comp[["A receber antigo", "A receber novo"]].max(axis=1)
    comp["Regra vencedora"] = comp.apply(
        lambda r: "Empate" if float(r.get("A receber antigo", 0.0)) == float(r.get("A receber novo", 0.0))
        else ("Cartilha nova" if float(r.get("A receber novo", 0.0)) > float(r.get("A receber antigo", 0.0)) else "Regra antiga"),
        axis=1,
    )
    comp["Diferença novo - antigo"] = comp["A receber novo"] - comp["A receber antigo"]
    return {
        "Regra_Antiga": df_antigo,
        "Cartilha_Nova": df_novo,
        "Comparativo": comp,
    }


def _old_rule_detail_by_month(mkey: str) -> pd.DataFrame:
    old_paid_before = _old_paid_max_before(mkey)
    old_levels = _visao_month_old_rule_levels(mkey)
    qtd_qual = len(old_levels)
    faixa_nome, precos = faixa_por_qtd(qtd_qual) if mkey != "12/2025" else (FAIXAS[-1][1], FAIXAS[-1][2])

    rows = []
    for cnpj, row in _visao_month_valid_rows(mkey).items():
        nivel = int(_supervisor_level(row))
        bank_old = _old_bank_cartilha_values(row)
        if nivel < 1 and bank_old is None:
            continue
        if bank_old is not None:
            cheio, ja_pago, receber = bank_old
        else:
            cheio = float(precos.get(nivel, 0.0))
            ja_pago = _old_prior_paid_value(old_paid_before, str(cnpj), mkey)
            receber = max(0.0, cheio - ja_pago)
        rows.append({
            "Mês": mkey,
            "CNPJ": str(cnpj),
            "Nome cliente": str(row.get("nome_cliente", "") or ""),
            "Nível antigo": nivel,
            "Faixa antiga": faixa_nome,
            "Banco BW apuração": float(row.get("old_cartilha_apuracao", 0.0) or 0.0),
            "Banco BX multiplicador": float(row.get("old_cartilha_multiplicador", 1.0) or 1.0),
            "Banco BY já pago": float(row.get("old_cartilha_ja_pago", 0.0) or 0.0),
            "Banco BZ previsão": float(row.get("old_cartilha_previsao", 0.0) or 0.0),
            "Valor cheio antigo": cheio,
            "Já pago ref antigo": ja_pago,
            "A receber antigo": receber,
            "Critérios antigos": str(row.get("criterios_atingidos_comiss", "") or ""),
        })
    return pd.DataFrame(rows)


def _nova_cashin_bands(factor: float = 1.0) -> List[Tuple[float, float]]:
    f = float(factor)
    return [
        (5000.0, 250.0 * f),
        (10000.0, 400.0 * f),
        (20000.0, 600.0 * f),
        (45000.0, 750.0 * f),
    ]


def _nova_spending_bands(factor: float) -> List[Tuple[float, float]]:
    return [
        (4000.0, 500.0 * float(factor)),
        (6000.0, 800.0 * float(factor)),
        (8000.0, 1100.0 * float(factor)),
        (12000.0, 1400.0 * float(factor)),
    ]


def _nova_tpv_bands(factor: float) -> List[Tuple[float, float]]:
    return [
        (3000.0, 600.0 * float(factor)),
        (8000.0, 1000.0 * float(factor)),
        (15000.0, 1600.0 * float(factor)),
        (30000.0, 2400.0 * float(factor)),
    ]


def _winner_amount_without_bonus(row: pd.Series) -> float:
    criterio = str(row.get("Critério vencedor novo", "") or "")
    if criterio == "Cash In":
        return float(pd.to_numeric(pd.Series([row.get("Valor Cash In", 0.0)]), errors="coerce").fillna(0.0).iloc[0])
    if criterio == "Spending":
        return float(pd.to_numeric(pd.Series([row.get("Valor Spending", 0.0)]), errors="coerce").fillna(0.0).iloc[0])
    if criterio == "C6 Pay":
        return float(pd.to_numeric(pd.Series([row.get("Valor C6 Pay", 0.0)]), errors="coerce").fillna(0.0).iloc[0])
    return 0.0


def _next_threshold_info(current_value: float, bands: List[Tuple[float, float]]) -> Optional[dict]:
    current_value = float(current_value or 0.0)
    for target, amount in bands:
        if current_value < float(target):
            return {
                "meta": float(target),
                "valor_proxima_faixa": float(amount),
                "faltante": float(target) - current_value,
            }
    return None


def _next_threshold_above_amount(current_value: float, bands: List[Tuple[float, float]], current_amount: float) -> Optional[dict]:
    current_value = float(current_value or 0.0)
    current_amount = float(current_amount or 0.0)
    for target, amount in bands:
        if current_value < float(target) and float(amount) > current_amount:
            return {
                "meta": float(target),
                "valor_proxima_faixa": float(amount),
                "faltante": float(target) - current_value,
                "ganho_adicional": float(amount) - current_amount,
            }
    return None


def _truthy_snapshot_value(v) -> bool:
    txt = "" if pd.isna(v) else str(v).strip().upper()
    return txt in {"1", "SIM", "TRUE", "S", "YES", "Y"}


def _parse_old_criteria_map(txt: str) -> Dict[str, int]:
    if not isinstance(txt, str) or not txt.strip():
        return {}
    out: Dict[str, int] = {}
    for key, num in re.findall(r"([A-ZÀ-Ü_ ]+):\s*(\d+)", txt.upper()):
        clean_key = " ".join(str(key).split())
        out[clean_key] = int(num)
    return out


def _focus_effort_rank(criterio: str, row: dict, current_metric: float, target_metric: float) -> int:
    criterio = str(criterio or "")
    current_metric = float(current_metric or 0.0)
    target_metric = float(target_metric or 0.0)
    ratio = (current_metric / target_metric) if target_metric > 0 else 0.0
    cart_entregue = bool(_parse_br_date_text(row.get("dt_entrega_cartao")))
    cart_ativado = bool(_parse_br_date_text(row.get("dt_ativ_cartao_cred")))
    pay_aprov = bool(_parse_br_date_text(row.get("dt_aprovacao_pay")))
    pay_inst = bool(_parse_br_date_text(row.get("dt_install_maq")))
    pay_ativ = bool(_parse_br_date_text(row.get("dt_ativacao_pay")))
    pay_ativa30 = _truthy_snapshot_value(row.get("c6pay_ativa_30"))

    if criterio == "Cash In":
        if ratio >= 0.9:
            return 1
        if ratio >= 0.75:
            return 2
        if ratio >= 0.5:
            return 3
        return 4

    if criterio == "Spending":
        base = 4
        if cart_ativado:
            base = 1
        elif cart_entregue:
            base = 2
        if ratio >= 0.9:
            base = max(1, base - 1)
        elif ratio < 0.35:
            base += 1
        return min(5, base)

    if criterio == "C6 Pay":
        if pay_ativ or pay_ativa30 or ratio >= 0.85:
            return 1
        if pay_inst:
            return 2
        if pay_aprov:
            return 3
        return 5

    if criterio == "Cartilha antiga":
        lvl = int(_supervisor_level(row))
        if lvl >= 3:
            return 2
        if lvl >= 1:
            return 3
        return 4

    return 5


def _focus_operational_hint(row: dict) -> Tuple[str, str]:
    pix_raw = _pix_clean_value(row.get("chaves_pix_forte", ""))
    tem_pix_cnpj = _pix_has_cnpj(pix_raw)
    cart_entregue = bool(_parse_br_date_text(row.get("dt_entrega_cartao")))
    cart_ativado = bool(_parse_br_date_text(row.get("dt_ativ_cartao_cred")))
    pay_aprov = bool(_parse_br_date_text(row.get("dt_aprovacao_pay")))
    pay_inst = bool(_parse_br_date_text(row.get("dt_install_maq")))
    pay_ativ = bool(_parse_br_date_text(row.get("dt_ativacao_pay")))
    pay_ativa30 = _truthy_snapshot_value(row.get("c6pay_ativa_30"))
    wallet_ok = _truthy_snapshot_value(row.get("wallet"))
    domicilio_c6 = contains_c6(row.get("banco_domicilio", ""))

    if not tem_pix_cnpj:
        return (
            "Cadastrar PIX CNPJ",
            "Cliente ainda sem PIX CNPJ válido; isso melhora a evolução operacional e ajuda a destravar próximos movimentos de relacionamento."
        )
    if cart_entregue and not cart_ativado:
        return (
            "Ativar cartão",
            "O cartão já foi entregue e ainda não foi ativado; esse é um passo operacional simples para destravar spending."
        )
    if pay_aprov and not pay_inst:
        return (
            "Concluir instalação da C6 Pay",
            "A proposta Pay já foi aprovada; concluir a instalação tende a ser o caminho operacional mais curto para monetizar a maquininha."
        )
    if pay_inst and not pay_ativ and not pay_ativa30:
        return (
            "Gerar a 1ª ativação da C6 Pay",
            "A maquininha já foi instalada; falta ativar uso/TPV para converter esse cliente em receita adicional."
        )
    if cart_ativado and not wallet_ok:
        return (
            "Cadastrar cartão no Wallet",
            "O cartão já está ativo; colocar no Wallet é um avanço operacional rápido e ajuda na qualificação geral."
        )
    if not domicilio_c6:
        return (
            "Trazer domicílio para o C6",
            "O cliente ainda não está domiciliado no C6; migrar o domicílio fortalece a recorrência de relacionamento."
        )
    return (
        "Manutenção / relacionamento",
        "Cliente sem alavanca óbvia de curto prazo; manter relacionamento e acompanhar novas movimentações."
    )


def _describe_focus_objective(criterio: str, meta: float, faixa: float, current_metric: float) -> str:
    criterio = str(criterio or "")
    if criterio == "Cash In":
        return f"Elevar o cash in de {br_money(current_metric)} para pelo menos {br_money(meta)} e migrar o cliente para a faixa de {br_money(faixa)}."
    if criterio == "Spending":
        return f"Elevar o spending do cartão de {br_money(current_metric)} para pelo menos {br_money(meta)} para acessar a faixa de {br_money(faixa)}."
    if criterio == "C6 Pay":
        return f"Levar o TPV M0 da C6 Pay de {br_money(current_metric)} para pelo menos {br_money(meta)} para alcançar a faixa de {br_money(faixa)}."
    if criterio == "Cartilha antiga":
        return f"Subir o cliente na cartilha antiga até a próxima faixa remunerada de {br_money(faixa)}."
    return "Gerar avanço operacional com potencial de aumentar a remuneração no próximo ciclo."


def _old_new_comparative_quadro(mkey: str) -> Dict[str, pd.DataFrame]:
    df_antigo = _old_rule_detail_by_month(mkey)
    df_novo = _cartilha_nova_detail_by_month(mkey)

    quadro_rows = []
    if not df_antigo.empty:
        grp_old = (
            df_antigo.groupby(["Nível antigo", "Valor cheio antigo"], dropna=False)
            .agg(
                Clientes=("CNPJ", "nunique"),
                Valor_cheio_total=("Valor cheio antigo", "sum"),
                A_receber_total=("A receber antigo", "sum"),
            )
            .reset_index()
            .sort_values(["Nível antigo", "Valor cheio antigo"])
        )
        for _, row in grp_old.iterrows():
            quadro_rows.append({
                "Regra": "Regra antiga",
                "Critério / nível": f"Nível {int(row['Nível antigo'])}",
                "Faixa remuneração": float(row["Valor cheio antigo"]),
                "Clientes": int(row["Clientes"]),
                "Valor cheio total": float(row["Valor_cheio_total"]),
                "A receber total": float(row["A_receber_total"]),
            })

    if not df_novo.empty:
        df_novo_q = df_novo[df_novo["Qualificado cartilha nova"].eq("SIM")].copy()
        if not df_novo_q.empty:
            df_novo_q["_faixa_remuneracao"] = df_novo_q.apply(_winner_amount_without_bonus, axis=1)
            grp_new = (
                df_novo_q.groupby(["Critério vencedor novo", "_faixa_remuneracao"], dropna=False)
                .agg(
                    Clientes=("CNPJ", "nunique"),
                    Valor_cheio_total=("Valor cheio novo", "sum"),
                    A_receber_total=("A receber novo", "sum"),
                )
                .reset_index()
                .sort_values(["Critério vencedor novo", "_faixa_remuneracao"])
            )
            for _, row in grp_new.iterrows():
                quadro_rows.append({
                    "Regra": "Cartilha nova",
                    "Critério / nível": str(row["Critério vencedor novo"]),
                    "Faixa remuneração": float(row["_faixa_remuneracao"]),
                    "Clientes": int(row["Clientes"]),
                    "Valor cheio total": float(row["Valor_cheio_total"]),
                    "A receber total": float(row["A_receber_total"]),
                })
        pix_bonus_total = float(pd.to_numeric(df_novo.get("Bônus PIX CNPJ", pd.Series(dtype=float)), errors="coerce").fillna(0.0).sum())
        if pix_bonus_total > 0:
            clientes_bonus = int((pd.to_numeric(df_novo.get("Bônus PIX CNPJ", pd.Series(dtype=float)), errors="coerce").fillna(0.0) > 0).sum())
            quadro_rows.append({
                "Regra": "Cartilha nova",
                "Critério / nível": "PIX CNPJ (bônus)",
                "Faixa remuneração": 15.0,
                "Clientes": clientes_bonus,
                "Valor cheio total": pix_bonus_total,
                "A receber total": pix_bonus_total,
            })

    df_quadro = pd.DataFrame(quadro_rows)

    old_lookup = {
        str(r["CNPJ"]): r for r in df_antigo.to_dict("records")
    } if not df_antigo.empty else {}
    new_lookup = {
        str(r["CNPJ"]): r for r in df_novo.to_dict("records")
    } if not df_novo.empty else {}

    old_levels = _visao_month_old_rule_levels(mkey)
    qtd_qual_old = len(old_levels)
    _, old_prices = faixa_por_qtd(qtd_qual_old) if mkey != "12/2025" else (FAIXAS[-1][1], FAIXAS[-1][2])

    foco_rows = []
    for cnpj, row in _visao_month_valid_rows(mkey).items():
        old_row = old_lookup.get(str(cnpj), {})
        new_row = new_lookup.get(str(cnpj), {})

        nome_cliente = str(row.get("nome_cliente", "") or "")
        old_level = int(pd.to_numeric(pd.Series([old_row.get("Nível antigo", _supervisor_level(row))]), errors="coerce").fillna(0).iloc[0])
        old_cheio = float(pd.to_numeric(pd.Series([old_row.get("Valor cheio antigo", 0.0)]), errors="coerce").fillna(0.0).iloc[0])
        old_receber = float(pd.to_numeric(pd.Series([old_row.get("A receber antigo", 0.0)]), errors="coerce").fillna(0.0).iloc[0])
        old_ja_pago = float(pd.to_numeric(pd.Series([old_row.get("Já pago ref antigo", 0.0)]), errors="coerce").fillna(0.0).iloc[0])
        old_next_level = min(4, old_level + 1) if old_level >= 1 else 1
        old_next_amount = float(old_prices.get(old_next_level, 0.0))
        old_gain = max(0.0, old_next_amount - old_cheio) if old_next_level > old_level else 0.0
        old_criterios = str(old_row.get("Critérios antigos", row.get("criterios_atingidos_comiss", "")) or "")

        fator = float(pd.to_numeric(pd.Series([new_row.get("Acelerador", 1.0)]), errors="coerce").fillna(1.0).iloc[0])
        cash_val = float(row.get("cash_in_valor", 0.0) or 0.0)
        spending_val = float(row.get("spending_total_mtd", 0.0) or 0.0)
        tpv_val = float(_nova_tpv_for_cartilha(row) or 0.0)
        cash_amt = _nova_cashin_amount(cash_val, fator)
        spending_amt = _nova_spending_amount(spending_val, fator)
        tpv_amt = _nova_tpv_amount(tpv_val, fator)
        current_new_best = max(cash_amt, spending_amt, tpv_amt)
        current_new_total = float(pd.to_numeric(pd.Series([new_row.get("Valor cheio novo", current_new_best)]), errors="coerce").fillna(current_new_best).iloc[0])
        current_new_receive = float(pd.to_numeric(pd.Series([new_row.get("A receber novo", 0.0)]), errors="coerce").fillna(0.0).iloc[0])
        current_new_paid = float(pd.to_numeric(pd.Series([new_row.get("Já pago ref novo", 0.0)]), errors="coerce").fillna(0.0).iloc[0])
        current_new_criterio = str(new_row.get("Critério vencedor novo", "Nenhum") or "Nenhum")

        oportunidades = []
        for criterio, valor_base, bands in [
            ("Cash In", cash_val, _nova_cashin_bands(fator)),
            ("Spending", spending_val, _nova_spending_bands(fator)),
            ("C6 Pay", tpv_val, _nova_tpv_bands(fator)),
        ]:
            next_info = _next_threshold_above_amount(valor_base, bands, current_new_best)
            if next_info:
                oportunidades.append({
                    "regra": "Cartilha nova",
                    "criterio": criterio,
                    "meta": float(next_info["meta"]),
                    "faltante": float(next_info["faltante"]),
                    "valor_proxima_faixa": float(next_info["valor_proxima_faixa"]),
                    "ganho_adicional": float(next_info["ganho_adicional"]),
                    "valor_atual_criterio": float(valor_base),
                    "effort_rank": _focus_effort_rank(criterio, row, valor_base, next_info["meta"]),
                })

        if old_gain > 0:
            oportunidades.append({
                "regra": "Cartilha antiga",
                "criterio": "Cartilha antiga",
                "meta": float(old_next_level),
                "faltante": 0.0,
                "valor_proxima_faixa": float(old_next_amount),
                "ganho_adicional": float(old_gain),
                "valor_atual_criterio": float(old_level),
                "effort_rank": _focus_effort_rank("Cartilha antiga", row, old_level, old_next_level),
            })

        oportunidades = sorted(
            oportunidades,
            key=lambda x: (-x["ganho_adicional"], x["effort_rank"], x["faltante"], x["criterio"])
        )
        melhor = oportunidades[0] if oportunidades else None
        alternativo = oportunidades[1] if len(oportunidades) > 1 else None

        hint_focus, hint_text = _focus_operational_hint(row)

        if melhor:
            if melhor["regra"] == "Cartilha antiga":
                objetivo = f"Subir o cliente do nível {old_level} para o nível {int(melhor['meta'])} na cartilha antiga."
                justificativa = (
                    f"Hoje o cliente está no nível {old_level} da cartilha antiga, com {br_money(old_cheio)} de valor cheio. "
                    f"Se subir para o nível {int(melhor['meta'])}, pode adicionar {br_money(melhor['ganho_adicional'])}. "
                    f"Critérios atuais: {old_criterios or 'sem detalhamento na base'}."
                )
                foco_principal = "Elevar nível na cartilha antiga"
            else:
                objetivo = _describe_focus_objective(
                    melhor["criterio"],
                    float(melhor["meta"]),
                    float(melhor["valor_proxima_faixa"]),
                    float(melhor["valor_atual_criterio"]),
                )
                justificativa_extra = ""
                if melhor["criterio"] == "Spending":
                    cart_entregue = bool(_parse_br_date_text(row.get("dt_entrega_cartao")))
                    cart_ativado = bool(_parse_br_date_text(row.get("dt_ativ_cartao_cred")))
                    if cart_ativado:
                        justificativa_extra = " O cartão já está ativado, então o caminho mais curto é aumentar giro/spending."
                    elif cart_entregue:
                        justificativa_extra = " O cartão já foi entregue, então ativar e girar o cartão pode ser a alavanca mais rápida."
                elif melhor["criterio"] == "C6 Pay":
                    if bool(_parse_br_date_text(row.get("dt_install_maq"))):
                        justificativa_extra = " A maquininha já está instalada, então falta converter isso em TPV."
                    elif bool(_parse_br_date_text(row.get("dt_aprovacao_pay"))):
                        justificativa_extra = " A proposta já foi aprovada, o foco é acelerar instalação/uso."
                justificativa = (
                    f"Hoje o cliente remunera {br_money(current_new_total)} na cartilha nova via {current_new_criterio}. "
                    f"Se atingir {melhor['criterio']} na próxima faixa, pode acrescentar {br_money(melhor['ganho_adicional'])}. "
                    f"Faltam {br_money(melhor['faltante'])} para chegar na meta de {br_money(melhor['meta'])}.{justificativa_extra}"
                )
                foco_principal = f"{melhor['criterio']} (cartilha nova)"
            status_receita = "Receita incremental disponível"
        else:
            status_receita = "Receita já capturada / manutenção"
            foco_principal = hint_focus
            objetivo = "Manter o cliente qualificado e trabalhar apenas avanços operacionais sem expectativa clara de receita adicional imediata."
            justificativa = (
                f"O cliente já capturou o potencial atual nas cartilhas antiga/nova (antiga: {br_money(old_cheio)}, nova: {br_money(current_new_total)}). "
                f"{hint_text}"
            )

        foco_rows.append({
            "Mês base": mkey,
            "CNPJ": str(cnpj),
            "Nome cliente": nome_cliente,
            "Status receita": status_receita,
            "Foco sugerido dia seguinte": foco_principal,
            "Objetivo do dia seguinte": objetivo,
            "Justificativa": justificativa,
            "Receita adicional possível antiga": float(old_gain),
            "Receita adicional possível nova": float(melhor["ganho_adicional"]) if melhor and melhor["regra"] == "Cartilha nova" else 0.0,
            "Receita adicional prioritária": float(melhor["ganho_adicional"]) if melhor else 0.0,
            "Regra foco principal": str(melhor["regra"]) if melhor else "Sem ganho adicional",
            "Critério atual cartilha nova": current_new_criterio,
            "Nível atual cartilha antiga": int(old_level),
            "Valor atual cartilha antiga": float(old_cheio),
            "Valor atual cartilha nova": float(current_new_total),
            "Já pago antigo": float(old_ja_pago),
            "Já pago novo": float(current_new_paid),
            "A receber antigo hoje": float(old_receber),
            "A receber novo hoje": float(current_new_receive),
            "Meta foco principal": float(melhor["meta"]) if melhor else 0.0,
            "Falta para meta": float(melhor["faltante"]) if melhor else 0.0,
            "Valor na próxima faixa": float(melhor["valor_proxima_faixa"]) if melhor else 0.0,
            "Foco alternativo": (
                f"{alternativo['criterio']} ({alternativo['regra']}) | +{br_money(alternativo['ganho_adicional'])}"
                if alternativo else ""
            ),
            "Critérios antigos": old_criterios,
            "Cash In atual": float(cash_val),
            "Spending atual": float(spending_val),
            "TPV C6 Pay atual": float(tpv_val),
            "PIX atual": str(_pix_clean_value(row.get("chaves_pix_forte", "")) or ""),
            "Tem PIX CNPJ": "SIM" if _pix_has_cnpj(row.get("chaves_pix_forte", "")) else "NÃO",
            "Cartão entregue": "SIM" if _parse_br_date_text(row.get("dt_entrega_cartao")) else "NÃO",
            "Cartão ativado": "SIM" if _parse_br_date_text(row.get("dt_ativ_cartao_cred")) else "NÃO",
            "C6 Pay aprovada": "SIM" if _parse_br_date_text(row.get("dt_aprovacao_pay")) else "NÃO",
            "C6 Pay instalada": "SIM" if _parse_br_date_text(row.get("dt_install_maq")) else "NÃO",
            "C6 Pay ativada": "SIM" if _parse_br_date_text(row.get("dt_ativacao_pay")) else "NÃO",
            "C6 Pay ativa 30": "SIM" if _truthy_snapshot_value(row.get("c6pay_ativa_30")) else "NÃO",
            "Domicílio C6": "SIM" if contains_c6(row.get("banco_domicilio", "")) else "NÃO",
            "Wallet": "SIM" if _truthy_snapshot_value(row.get("wallet")) else "NÃO",
        })

    df_foco = pd.DataFrame(foco_rows)
    if not df_foco.empty:
        df_foco = df_foco.sort_values(
            ["Receita adicional prioritária", "Falta para meta", "Nome cliente"],
            ascending=[False, True, True],
            na_position="last",
        ).reset_index(drop=True)

    sheets = {
        "Resumo_Quadro": df_quadro,
        "Foco_Proximo_Mes": df_foco,
        "Regra_Antiga": df_antigo,
        "Cartilha_Nova": df_novo,
    }
    return {"quadro": df_quadro, "foco": df_foco, "sheets": sheets}


def _focus_sugerido_base_vigente(base_mkey: str) -> Dict[str, pd.DataFrame]:
    quadro_pack = _old_new_comparative_quadro(base_mkey)
    df_foco = quadro_pack.get("foco", pd.DataFrame()).copy()
    if df_foco.empty:
        return {
            "foco": pd.DataFrame(),
            "resumo": pd.DataFrame(),
            "sheets": {
                "Resumo_Quadro": quadro_pack.get("quadro", pd.DataFrame()),
                "Foco_Base_Vigente": pd.DataFrame(),
                "Resumo_Foco_Base_Vigente": pd.DataFrame(),
                "Regra_Antiga": quadro_pack.get("sheets", {}).get("Regra_Antiga", pd.DataFrame()),
                "Cartilha_Nova": quadro_pack.get("sheets", {}).get("Cartilha_Nova", pd.DataFrame()),
            },
        }

    open_month_keys = set(_vigent_open_month_keys(base_mkey))
    current_rows = _visao_month_rows(base_mkey)

    meta_rows = []
    for _, foco_row in df_foco.iterrows():
        cnpj = str(foco_row.get("CNPJ", "") or "")
        snap = current_rows.get(cnpj, {})
        dt_conta_txt = str(snap.get("dt_conta_criada", "") or "")
        dt_conta = _parse_br_date_text(dt_conta_txt)
        if not dt_conta:
            continue
        conta_mkey = fmt_month(dt_conta)
        if conta_mkey not in open_month_keys:
            continue

        tel1, tel2 = _focus_phone_pair(snap)
        meta_rows.append({
            **foco_row.to_dict(),
            "Mês abertura": conta_mkey,
            "Data conta aberta": dt_conta_txt,
            "Fundação empresa": str(snap.get("dt_fundacao_empresa", "") or ""),
            "Telefone 1": tel1,
            "Telefone 2": tel2,
        })

    df_vigente = pd.DataFrame(meta_rows)
    if df_vigente.empty:
        resumo = pd.DataFrame()
    else:
        df_vigente = df_vigente.sort_values(
            [
                "Receita adicional prioritária",
                "Receita adicional possível nova",
                "Receita adicional possível antiga",
                "Falta para meta",
                "Nome cliente",
            ],
            ascending=[False, False, False, True, True],
            na_position="last",
        ).reset_index(drop=True)

        resumo = (
            df_vigente.groupby(["Status receita", "Foco sugerido dia seguinte", "Regra foco principal"], dropna=False)
            .agg(
                Clientes=("CNPJ", "nunique"),
                Receita_potencial=("Receita adicional prioritária", "sum"),
            )
            .reset_index()
            .sort_values(["Receita_potencial", "Clientes", "Foco sugerido dia seguinte"], ascending=[False, False, True])
        )

    sheets = {
        "Resumo_Quadro": quadro_pack.get("quadro", pd.DataFrame()),
        "Foco_Base_Vigente": df_vigente,
        "Resumo_Foco_Base_Vigente": resumo,
        "Regra_Antiga": quadro_pack.get("sheets", {}).get("Regra_Antiga", pd.DataFrame()),
        "Cartilha_Nova": quadro_pack.get("sheets", {}).get("Cartilha_Nova", pd.DataFrame()),
    }
    return {"foco": df_vigente, "resumo": resumo, "sheets": sheets}


def compute_campanha_tri() -> pd.DataFrame:
    rows = []

    for mkey in ["04/2026", "05/2026", "06/2026"]:
        meta = CAMPANHA_2TRI_METAS.get(mkey, {})
        bucket_months = _campaign_bucket_months(mkey)
        bucket_rows = []
        current_rows = _visao_month_rows(mkey)

        current_valid_rows = {}
        for cnpj, row in current_rows.items():
            if not _c6_visao_row_eligible_pj(row):
                continue
            current_valid_rows[str(cnpj)] = row

        for bm in bucket_months:
            for cnpj, row in _visao_month_rows(bm).items():
                if not _c6_visao_row_eligible_pj(row):
                    continue
                bucket_rows.append((cnpj, row))

        bucket_by_cnpj = {}
        for cnpj, row in bucket_rows:
            bucket_by_cnpj[cnpj] = row

        aberturas_mes = int(_visao_month_openings_count(mkey))

        detail_nova_mes = _cartilha_nova_detail_by_month(mkey)
        if detail_nova_mes.empty:
            qualificados_mes = 0
        else:
            qualificados_mes = int(detail_nova_mes["Qualificado cartilha nova"].astype(str).str.upper().eq("SIM").sum())

        ativ_pay_mes = 0
        for row in current_valid_rows.values():
            if float(_nova_tpv_for_cartilha(row) or 0.0) > 1000.0:
                ativ_pay_mes += 1

        bucket_total = len(bucket_by_cnpj)
        meta_qual = int(meta.get("qualificacao", 0))
        perc_qual = (qualificados_mes / meta_qual) if meta_qual > 0 else 0.0
        bateu_percentual = perc_qual >= float(CAMPANHA_2TRI_METAS["TRI"]["perc_min"])
        bateu_mensal = (
            aberturas_mes >= int(meta.get("abertura", 0))
            and qualificados_mes >= meta_qual
            and ativ_pay_mes >= int(meta.get("ativacao_pay", 0))
            and bateu_percentual
        )

        rows.append({
            "Mês": mkey,
            "Aberturas": aberturas_mes,
            "Meta Aberturas": int(meta.get("abertura", 0)),
            "Balde válido": bucket_total,
            "Qualificados": qualificados_mes,
            "Meta Qualificados": int(meta.get("qualificacao", 0)),
            "% Qualificação": perc_qual,
            "% Mínimo": float(CAMPANHA_2TRI_METAS["TRI"]["perc_min"]),
            "Ativações C6 Pay": ativ_pay_mes,
            "Meta Ativações C6 Pay": int(meta.get("ativacao_pay", 0)),
            "Elegível pagamento mensal": "SIM" if bateu_mensal else "NÃO",
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    tri = {
        "Mês": "TRI",
        "Aberturas": int(df["Aberturas"].sum()),
        "Meta Aberturas": int(CAMPANHA_2TRI_METAS["TRI"]["abertura"]),
        "Balde válido": int(df["Balde válido"].sum()),
        "Qualificados": int(df["Qualificados"].sum()),
        "Meta Qualificados": int(CAMPANHA_2TRI_METAS["TRI"]["qualificacao"]),
        "% Qualificação": (float(df["Qualificados"].sum()) / float(max(int(CAMPANHA_2TRI_METAS["TRI"]["qualificacao"]), 1))),
        "% Mínimo": float(CAMPANHA_2TRI_METAS["TRI"]["perc_min"]),
        "Ativações C6 Pay": int(df["Ativações C6 Pay"].sum()),
        "Meta Ativações C6 Pay": int(CAMPANHA_2TRI_METAS["TRI"]["ativacao_pay"]),
        "Elegível pagamento mensal": "",
    }
    tri_ok = (
        tri["Aberturas"] >= tri["Meta Aberturas"]
        and tri["Qualificados"] >= tri["Meta Qualificados"]
        and tri["Ativações C6 Pay"] >= tri["Meta Ativações C6 Pay"]
        and tri["% Qualificação"] >= tri["% Mínimo"]
    )
    tri["Elegível pagamento mensal"] = "SIM" if tri_ok else "NÃO"
    return pd.concat([df, pd.DataFrame([tri])], ignore_index=True)


# =========================================================
# SUPERVISOR CC6
# =========================================================
def _parse_br_date_text(value) -> Optional[dt.date]:
    if value is None or pd.isna(value):
        return None
    txt = str(value).strip()
    if not txt:
        return None
    try:
        return dt.datetime.strptime(txt, "%d/%m/%Y").date()
    except Exception:
        try:
            parsed = pd.to_datetime(txt, errors="coerce", dayfirst=True)
            return None if pd.isna(parsed) else parsed.date()
        except Exception:
            return None


def _supervisor_valid_rows(cmap: Dict[str, dict]) -> Dict[str, dict]:
    valid = {}
    for cnpj, row in (cmap or {}).items():
        tipo = str(row.get("tipo_pessoa", "")).upper()
        status = str(row.get("status_cc", "")).upper()
        if tipo != "PJ":
            continue
        if "MEI" in tipo:
            continue
        if status in {"BLOQUEADA", "DESATIVADA", "ENCERRADA"}:
            continue
        valid[cnpj] = row
    return valid


def _supervisor_level(row: dict) -> int:
    lvl_by = int(pd.to_numeric(pd.Series([row.get("fl_qualificado_comiss")]), errors="coerce").fillna(0).iloc[0])
    lvl_by = lvl_by if 1 <= lvl_by <= 4 else 0
    lvl_crit = parse_level_from_criterios(str(row.get("criterios_atingidos_comiss", "") or ""))
    return max(lvl_by, lvl_crit)


def _supervisor_cartilha_flags(row: dict) -> dict:
    cash_amt = _nova_cashin_amount(float(row.get("cash_in_valor", 0.0) or 0.0), 1.0)
    spending_ok = "ATRAS" not in str(row.get("status_pagamento_fatura", "")).upper()
    spending_amt = _nova_spending_amount(float(row.get("spending_total_mtd", 0.0) or 0.0), 1.0) if spending_ok else 0.0
    tpv_amt = _nova_tpv_amount(_nova_tpv_for_cartilha(row), 1.0)
    return {
        "qualificado": max(cash_amt, spending_amt, tpv_amt) > 0,
        "spending": spending_amt > 0,
        "tpv": tpv_amt > 0,
    }


def _supervisor_reward_by_tier(total: int, tiers: List[Tuple[int, float]]) -> float:
    for target, reward in tiers:
        if int(total) >= int(target):
            return float(reward)
    return 0.0


def compute_supervisor_cc6_meta() -> Tuple[pd.DataFrame, pd.DataFrame, dict]:
    visao_store = _load_visao_month_snapshot()
    month_rows = []

    q_open = set()
    q_install = set()
    q_pay = set()
    q_qual = set()
    q_dom = set()
    q_spending = set()
    q_level4 = set()
    q_entregue = set()
    q_cartao_ativ = set()
    q_wallet = set()
    pix_base_total = 0
    pix_num_total = 0

    for mkey in SUPERVISOR_CC6_MESES:
        current_rows = _supervisor_valid_rows(visao_store.get(mkey, {}) or {})
        start, end = _month_range(mkey)

        abertas_mes = set()
        install_mes = set()
        ativ_pay_mes = set()
        qual_mes = set()
        dom_mes = set()
        spending_mes = set()
        nivel4_mes = set()
        entregue_mes = set()
        ativ_cartao_mes = set()
        pix_base_mes = 0
        pix_num_mes = 0

        for cnpj, row in current_rows.items():
            dt_abertura = _parse_br_date_text(row.get("dt_conta_criada"))
            dt_install = _parse_br_date_text(row.get("dt_install_maq"))
            dt_entrega = _parse_br_date_text(row.get("dt_entrega_cartao"))
            dt_cartao = _parse_br_date_text(row.get("dt_ativ_cartao_cred"))
            mes_ref = str(row.get("mes_ref_comiss", "") or "").strip().upper()
            pix_raw = _pix_clean_value(row.get("chaves_pix_forte", ""))
            wallet_raw = str(row.get("wallet", "") or "").upper()
            level = _supervisor_level(row)
            criterios = str(row.get("criterios_atingidos_comiss", "") or "")
            tpv_m0 = float(pd.to_numeric(pd.Series([row.get("tpv_m0")]), errors="coerce").fillna(0.0).iloc[0])

            if start and end and dt_abertura and start <= dt_abertura <= end:
                abertas_mes.add(cnpj)
                q_open.add(cnpj)
            if start and end and dt_install and start <= dt_install <= end:
                install_mes.add(cnpj)
                q_install.add(cnpj)
            if tpv_m0 > 1000:
                ativ_pay_mes.add(cnpj)
                q_pay.add(cnpj)
            if level >= 1:
                qual_mes.add(cnpj)
                q_qual.add(cnpj)
            if _criterio_score(criterios, "DOMICILIO") > 0:
                dom_mes.add(cnpj)
                q_dom.add(cnpj)
            if _criterio_score(criterios, "SPENDING") > 0:
                spending_mes.add(cnpj)
                q_spending.add(cnpj)
            if level >= 4:
                nivel4_mes.add(cnpj)
                q_level4.add(cnpj)
            if start and end and dt_entrega and start <= dt_entrega <= end:
                entregue_mes.add(cnpj)
                q_entregue.add(cnpj)
            if start and end and dt_cartao and start <= dt_cartao <= end:
                ativ_cartao_mes.add(cnpj)
                q_cartao_ativ.add(cnpj)
            if mes_ref in {"M0", "M1", "M2"}:
                pix_base_mes += 1
                pix_base_total += 1
                if _pix_has_cnpj(pix_raw):
                    pix_num_mes += 1
                    pix_num_total += 1
            if wallet_raw not in {"", "0", "NAO", "NÃO", "FALSE", "NAN", "NONE"}:
                q_wallet.add(cnpj)

        entregues_count = len(entregue_mes)
        ativados_count = len(ativ_cartao_mes)
        month_rows.append({
            "Mês": mkey,
            "Contas abertas": len(abertas_mes),
            "Contas qualificadas": len(qual_mes),
            "Instalações C6 Pay": len(install_mes),
            "C6 Pay ativadas": len(ativ_pay_mes),
            "PIX CNPJ %": (pix_num_mes / pix_base_mes) if pix_base_mes > 0 else 0.0,
            "Domicílio qualificado": len(dom_mes),
            "Spending qualificado": len(spending_mes),
            "Cartões entregues": entregues_count,
            "Cartões ativados": ativados_count,
            "Ativação cartão %": (ativados_count / entregues_count) if entregues_count > 0 else 0.0,
            "Nível 4": len(nivel4_mes),
        })

    qual_reward = _supervisor_reward_by_tier(
        len(q_qual), SUPERVISOR_CC6_METAS["contas_qualificadas"]["faixas"]
    )
    card_pct = (len(q_cartao_ativ) / len(q_entregue)) if len(q_entregue) > 0 else 0.0
    wallet_pct = (len(q_wallet) / len(q_cartao_ativ)) if len(q_cartao_ativ) > 0 else 0.0
    pix_pct = (pix_num_total / pix_base_total) if pix_base_total > 0 else 0.0

    indicadores = [
        {
            "Indicador": "Contas abertas",
            "Realizado": len(q_open),
            "Meta": SUPERVISOR_CC6_METAS["contas_abertas"]["meta"],
            "Atingimento": (len(q_open) / SUPERVISOR_CC6_METAS["contas_abertas"]["meta"]) if SUPERVISOR_CC6_METAS["contas_abertas"]["meta"] > 0 else 0.0,
            "Prêmio": SUPERVISOR_CC6_METAS["contas_abertas"]["premio"],
            "Recebe": SUPERVISOR_CC6_METAS["contas_abertas"]["premio"] if len(q_open) >= SUPERVISOR_CC6_METAS["contas_abertas"]["meta"] else 0.0,
            "Status": "Batida" if len(q_open) >= SUPERVISOR_CC6_METAS["contas_abertas"]["meta"] else "Em aberto",
        },
        {
            "Indicador": "Contas qualificadas",
            "Realizado": len(q_qual),
            "Meta": 700,
            "Atingimento": (len(q_qual) / 700.0) if 700 > 0 else 0.0,
            "Prêmio": qual_reward,
            "Recebe": qual_reward,
            "Status": "Batida" if qual_reward > 0 else "Em aberto",
        },
        {
            "Indicador": "Instalação C6 Pay",
            "Realizado": len(q_install),
            "Meta": SUPERVISOR_CC6_METAS["instalacao_c6pay"]["meta"],
            "Atingimento": (len(q_install) / SUPERVISOR_CC6_METAS["instalacao_c6pay"]["meta"]) if SUPERVISOR_CC6_METAS["instalacao_c6pay"]["meta"] > 0 else 0.0,
            "Prêmio": SUPERVISOR_CC6_METAS["instalacao_c6pay"]["premio"],
            "Recebe": SUPERVISOR_CC6_METAS["instalacao_c6pay"]["premio"] if len(q_install) >= SUPERVISOR_CC6_METAS["instalacao_c6pay"]["meta"] else 0.0,
            "Status": "Batida" if len(q_install) >= SUPERVISOR_CC6_METAS["instalacao_c6pay"]["meta"] else "Em aberto",
        },
        {
            "Indicador": "C6 Pay ativada",
            "Realizado": len(q_pay),
            "Meta": SUPERVISOR_CC6_METAS["c6pay_ativada"]["meta"],
            "Atingimento": (len(q_pay) / SUPERVISOR_CC6_METAS["c6pay_ativada"]["meta"]) if SUPERVISOR_CC6_METAS["c6pay_ativada"]["meta"] > 0 else 0.0,
            "Prêmio": SUPERVISOR_CC6_METAS["c6pay_ativada"]["premio"],
            "Recebe": SUPERVISOR_CC6_METAS["c6pay_ativada"]["premio"] if len(q_pay) >= SUPERVISOR_CC6_METAS["c6pay_ativada"]["meta"] else 0.0,
            "Status": "Batida" if len(q_pay) >= SUPERVISOR_CC6_METAS["c6pay_ativada"]["meta"] else "Em aberto",
        },
        {
            "Indicador": "Chave Pix CNPJ",
            "Realizado": pix_pct,
            "Meta": SUPERVISOR_CC6_METAS["pix_cnpj"]["meta"],
            "Atingimento": (pix_pct / SUPERVISOR_CC6_METAS["pix_cnpj"]["meta"]) if SUPERVISOR_CC6_METAS["pix_cnpj"]["meta"] > 0 else 0.0,
            "Prêmio": SUPERVISOR_CC6_METAS["pix_cnpj"]["premio"],
            "Recebe": SUPERVISOR_CC6_METAS["pix_cnpj"]["premio"] if pix_pct >= SUPERVISOR_CC6_METAS["pix_cnpj"]["meta"] and pix_base_total > 0 else 0.0,
            "Status": "Batida" if pix_pct >= SUPERVISOR_CC6_METAS["pix_cnpj"]["meta"] and pix_base_total > 0 else "Em aberto",
        },
        {
            "Indicador": "C6 domicílio qualificado",
            "Realizado": len(q_dom),
            "Meta": SUPERVISOR_CC6_METAS["domicilio_qualificado"]["meta"],
            "Atingimento": (len(q_dom) / SUPERVISOR_CC6_METAS["domicilio_qualificado"]["meta"]) if SUPERVISOR_CC6_METAS["domicilio_qualificado"]["meta"] > 0 else 0.0,
            "Prêmio": SUPERVISOR_CC6_METAS["domicilio_qualificado"]["premio"],
            "Recebe": SUPERVISOR_CC6_METAS["domicilio_qualificado"]["premio"] if len(q_dom) >= SUPERVISOR_CC6_METAS["domicilio_qualificado"]["meta"] else 0.0,
            "Status": "Batida" if len(q_dom) >= SUPERVISOR_CC6_METAS["domicilio_qualificado"]["meta"] else "Em aberto",
        },
        {
            "Indicador": "Spending qualificado",
            "Realizado": len(q_spending),
            "Meta": SUPERVISOR_CC6_METAS["spending_qualificado"]["meta"],
            "Atingimento": (len(q_spending) / SUPERVISOR_CC6_METAS["spending_qualificado"]["meta"]) if SUPERVISOR_CC6_METAS["spending_qualificado"]["meta"] > 0 else 0.0,
            "Prêmio": SUPERVISOR_CC6_METAS["spending_qualificado"]["premio"],
            "Recebe": SUPERVISOR_CC6_METAS["spending_qualificado"]["premio"] if len(q_spending) >= SUPERVISOR_CC6_METAS["spending_qualificado"]["meta"] else 0.0,
            "Status": "Batida" if len(q_spending) >= SUPERVISOR_CC6_METAS["spending_qualificado"]["meta"] else "Em aberto",
        },
        {
            "Indicador": "Wallet",
            "Realizado": wallet_pct,
            "Meta": SUPERVISOR_CC6_METAS["wallet"]["meta"],
            "Atingimento": 0.0,
            "Prêmio": SUPERVISOR_CC6_METAS["wallet"]["premio"],
            "Recebe": 0.0,
            "Status": "Sem base",
        },
        {
            "Indicador": "Ativação cartão",
            "Realizado": card_pct,
            "Meta": SUPERVISOR_CC6_METAS["ativacao_cartao"]["meta"],
            "Atingimento": (card_pct / SUPERVISOR_CC6_METAS["ativacao_cartao"]["meta"]) if SUPERVISOR_CC6_METAS["ativacao_cartao"]["meta"] > 0 else 0.0,
            "Prêmio": SUPERVISOR_CC6_METAS["ativacao_cartao"]["premio"],
            "Recebe": SUPERVISOR_CC6_METAS["ativacao_cartao"]["premio"] if card_pct >= SUPERVISOR_CC6_METAS["ativacao_cartao"]["meta"] and len(q_entregue) > 0 else 0.0,
            "Status": "Batida" if card_pct >= SUPERVISOR_CC6_METAS["ativacao_cartao"]["meta"] and len(q_entregue) > 0 else "Em aberto",
        },
        {
            "Indicador": "Nível 4 acima de 300",
            "Realizado": len(q_level4),
            "Meta": SUPERVISOR_CC6_METAS["nivel4"]["meta"],
            "Atingimento": (len(q_level4) / SUPERVISOR_CC6_METAS["nivel4"]["meta"]) if SUPERVISOR_CC6_METAS["nivel4"]["meta"] > 0 else 0.0,
            "Prêmio": SUPERVISOR_CC6_METAS["nivel4"]["premio"],
            "Recebe": SUPERVISOR_CC6_METAS["nivel4"]["premio"] if len(q_level4) > SUPERVISOR_CC6_METAS["nivel4"]["meta"] else 0.0,
            "Status": "Batida" if len(q_level4) > SUPERVISOR_CC6_METAS["nivel4"]["meta"] else "Em aberto",
        },
    ]

    summary = {
        "recebe_total": float(sum(item["Recebe"] for item in indicadores)),
        "metas_batidas": int(sum(1 for item in indicadores if float(item["Recebe"]) > 0)),
        "qtd_indicadores": int(len(indicadores)),
        "contas_qualificadas": int(len(q_qual)),
        "contas_abertas": int(len(q_open)),
        "pix_pct": float(pix_pct),
        "pix_base": int(pix_base_total),
        "pix_cnpj": int(pix_num_total),
        "cartoes_entregues": int(len(q_entregue)),
        "cartoes_ativados": int(len(q_cartao_ativ)),
    }
    return pd.DataFrame(indicadores), pd.DataFrame(month_rows), summary


# =========================================================
# SUPERVISOR C6 - AJUSTES
# =========================================================
def _supervisor_c6_default_monthly_meta() -> dict:
    return {
        "contas_abertas_meta": int(SUPERVISOR_C6_METAS["contas_abertas"]["meta"]),
        "contas_abertas_premio": float(SUPERVISOR_C6_METAS["contas_abertas"]["premio"]),
        "contas_qualificadas_faixas": [
            {"meta": int(meta), "premio": float(premio)}
            for meta, premio in SUPERVISOR_C6_METAS["contas_qualificadas"]["faixas"]
        ],
        "instalacao_c6pay_meta": int(SUPERVISOR_C6_METAS["instalacao_c6pay"]["meta"]),
        "instalacao_c6pay_premio": float(SUPERVISOR_C6_METAS["instalacao_c6pay"]["premio"]),
        "c6pay_ativada_meta": int(SUPERVISOR_C6_METAS["c6pay_ativada"]["meta"]),
        "c6pay_ativada_premio": float(SUPERVISOR_C6_METAS["c6pay_ativada"]["premio"]),
        "pix_cnpj_meta": float(SUPERVISOR_C6_METAS["pix_cnpj"]["meta"]),
        "pix_cnpj_premio": float(SUPERVISOR_C6_METAS["pix_cnpj"]["premio"]),
        "domicilio_qualificado_meta": int(SUPERVISOR_C6_METAS["domicilio_qualificado"]["meta"]),
        "domicilio_qualificado_premio": float(SUPERVISOR_C6_METAS["domicilio_qualificado"]["premio"]),
        "spending_qualificado_meta": int(SUPERVISOR_C6_METAS["spending_qualificado"]["meta"]),
        "spending_qualificado_premio": float(SUPERVISOR_C6_METAS["spending_qualificado"]["premio"]),
        "wallet_meta": float(SUPERVISOR_C6_METAS["wallet"]["meta"]),
        "wallet_premio": float(SUPERVISOR_C6_METAS["wallet"]["premio"]),
        "ativacao_cartao_meta": float(SUPERVISOR_C6_METAS["ativacao_cartao"]["meta"]),
        "ativacao_cartao_premio": float(SUPERVISOR_C6_METAS["ativacao_cartao"]["premio"]),
        "nivel4_meta": int(SUPERVISOR_C6_METAS["nivel4"]["meta"]),
        "nivel4_premio": float(SUPERVISOR_C6_METAS["nivel4"]["premio"]),
    }


def _normalize_supervisor_c6_monthly_meta(raw: Optional[dict] = None) -> dict:
    base = _supervisor_c6_default_monthly_meta()
    raw = raw if isinstance(raw, dict) else {}
    for key, default_val in list(base.items()):
        if key == "contas_qualificadas_faixas":
            faixas = raw.get(key, default_val)
            clean = []
            if isinstance(faixas, list):
                for item in faixas:
                    if not isinstance(item, dict):
                        continue
                    meta = int(pd.to_numeric(pd.Series([item.get("meta")]), errors="coerce").fillna(0).iloc[0])
                    premio = float(pd.to_numeric(pd.Series([item.get("premio")]), errors="coerce").fillna(0.0).iloc[0])
                    if meta > 0:
                        clean.append({"meta": meta, "premio": premio})
            base[key] = sorted(clean or default_val, key=lambda x: int(x["meta"]), reverse=True)
        elif key.endswith("_meta") and key in {"pix_cnpj_meta", "wallet_meta", "ativacao_cartao_meta"}:
            val = float(pd.to_numeric(pd.Series([raw.get(key, default_val)]), errors="coerce").fillna(default_val).iloc[0])
            base[key] = val / 100.0 if val > 1 else val
        elif key.endswith("_premio"):
            base[key] = float(pd.to_numeric(pd.Series([raw.get(key, default_val)]), errors="coerce").fillna(default_val).iloc[0])
        else:
            base[key] = int(pd.to_numeric(pd.Series([raw.get(key, default_val)]), errors="coerce").fillna(default_val).iloc[0])
    return base


def _load_supervisor_c6_monthly_metas() -> dict:
    store = safe_json_load(SUPERVISOR_C6_MONTHLY_METAS_PATH, default={}) or {}
    if not isinstance(store, dict):
        store = {}
    if "04/2026" not in store:
        store["04/2026"] = _supervisor_c6_default_monthly_meta()
        safe_json_save(SUPERVISOR_C6_MONTHLY_METAS_PATH, store)
    return {str(k): _normalize_supervisor_c6_monthly_meta(v) for k, v in store.items()}


def _save_supervisor_c6_monthly_meta(mkey: str, meta: dict):
    store = _load_supervisor_c6_monthly_metas()
    store[str(mkey)] = _normalize_supervisor_c6_monthly_meta(meta)
    safe_json_save(SUPERVISOR_C6_MONTHLY_METAS_PATH, store)


def _supervisor_c6_meta_for_month(mkey: str) -> dict:
    store = _load_supervisor_c6_monthly_metas()
    if mkey in store:
        return store[mkey]
    prev_keys = [k for k in store.keys() if month_key_str(k) <= month_key_str(mkey)]
    if prev_keys:
        return store[sorted(prev_keys, key=month_key_str)[-1]]
    return _supervisor_c6_default_monthly_meta()


def _supervisor_c6_qual_reward(total: int, meta: dict) -> Tuple[float, float, str, int]:
    faixas = meta.get("contas_qualificadas_faixas") or []
    faixas = sorted(faixas, key=lambda x: int(x.get("meta", 0)), reverse=True)
    for faixa in faixas:
        alvo = int(faixa.get("meta", 0) or 0)
        premio = float(faixa.get("premio", 0.0) or 0.0)
        if alvo > 0 and total >= alvo:
            faixa_txt = " | ".join(f"{br_int(int(f.get('meta', 0)))} -> {br_money(float(f.get('premio', 0.0)))}" for f in sorted(faixas, key=lambda x: int(x.get("meta", 0))))
            return premio, premio, faixa_txt, int(min(int(f.get("meta", 0)) for f in faixas if int(f.get("meta", 0)) > 0))
    faixa_txt = " | ".join(f"{br_int(int(f.get('meta', 0)))} -> {br_money(float(f.get('premio', 0.0)))}" for f in sorted(faixas, key=lambda x: int(x.get("meta", 0))))
    premio_ref = float(sorted(faixas, key=lambda x: int(x.get("meta", 0)))[0].get("premio", 0.0)) if faixas else 0.0
    meta_min = int(min([int(f.get("meta", 0)) for f in faixas if int(f.get("meta", 0)) > 0] or [0]))
    return 0.0, premio_ref, faixa_txt, meta_min


def _supervisor_c6_indicator(label: str, realizado, meta_val, premio, tipo: str, *, base_ok: bool = True, greater_than: bool = False, faixa: str = "") -> dict:
    realizado_num = float(realizado or 0)
    meta_num = float(meta_val or 0)
    ating = (realizado_num / meta_num) if meta_num > 0 else 0.0
    hit = base_ok and (realizado_num > meta_num if greater_than else realizado_num >= meta_num)
    return {
        "Indicador": label,
        "Realizado_num": realizado_num,
        "Meta_num": meta_num,
        "Atingimento_num": ating,
        "Premio_num": float(premio or 0.0),
        "Recebe_num": float(premio or 0.0) if hit else 0.0,
        "Faixa": faixa,
        "Status": "Batida" if hit else ("Sem base" if not base_ok else "Em aberto"),
        "Tipo": tipo,
    }


def _supervisor_reapply_monthly_meta(snapshot: dict, mkey: str) -> dict:
    snap = dict(snapshot or {})
    meta = _supervisor_c6_meta_for_month(mkey)
    contas_abertas = int(snap.get("contas_abertas", 0) or 0)
    contas_qualificadas = int(snap.get("contas_qualificadas", 0) or 0)
    instalacoes_c6pay = int(snap.get("instalacoes_c6pay", 0) or 0)
    c6pay_ativadas = int(snap.get("c6pay_ativadas", 0) or 0)
    pix_pct = float(snap.get("pix_pct", 0.0) or 0.0)
    pix_base = int(snap.get("pix_base", 0) or 0)
    domicilio_qualificado = int(snap.get("domicilio_qualificado", 0) or 0)
    spending_qualificado = int(snap.get("spending_qualificado", 0) or 0)
    wallet_pct = float(snap.get("wallet_pct", 0.0) or 0.0)
    ativacao_cartao_pct = float(snap.get("ativacao_cartao_pct", 0.0) or 0.0)
    cartoes_entregues = int(snap.get("cartoes_entregues", 0) or 0)
    nivel4 = int(snap.get("nivel4", 0) or 0)
    qual_reward, qual_premio_ref, qual_faixa, qual_meta_min = _supervisor_c6_qual_reward(contas_qualificadas, meta)
    indicadores = [
        _supervisor_c6_indicator("Contas abertas", contas_abertas, meta["contas_abertas_meta"], meta["contas_abertas_premio"], "inteiro"),
        {
            "Indicador": "Contas qualificadas",
            "Realizado_num": contas_qualificadas,
            "Meta_num": qual_meta_min,
            "Atingimento_num": (contas_qualificadas / qual_meta_min) if qual_meta_min > 0 else 0.0,
            "Premio_num": qual_premio_ref,
            "Recebe_num": qual_reward,
            "Faixa": qual_faixa,
            "Status": "Batida" if qual_reward > 0 else "Em aberto",
            "Tipo": "inteiro",
        },
        _supervisor_c6_indicator("Instalacao C6 Pay", instalacoes_c6pay, meta["instalacao_c6pay_meta"], meta["instalacao_c6pay_premio"], "inteiro"),
        _supervisor_c6_indicator("C6 Pay ativada", c6pay_ativadas, meta["c6pay_ativada_meta"], meta["c6pay_ativada_premio"], "inteiro"),
        _supervisor_c6_indicator("Chave Pix CNPJ", pix_pct, meta["pix_cnpj_meta"], meta["pix_cnpj_premio"], "percentual", base_ok=pix_base > 0),
        _supervisor_c6_indicator("Domicilio qualificado", domicilio_qualificado, meta["domicilio_qualificado_meta"], meta["domicilio_qualificado_premio"], "inteiro"),
        _supervisor_c6_indicator("Spending qualificado", spending_qualificado, meta["spending_qualificado_meta"], meta["spending_qualificado_premio"], "inteiro"),
        _supervisor_c6_indicator("Wallet", wallet_pct, meta["wallet_meta"], meta["wallet_premio"], "percentual", base_ok=cartoes_entregues > 0),
        _supervisor_c6_indicator("Ativacao cartao", ativacao_cartao_pct, meta["ativacao_cartao_meta"], meta["ativacao_cartao_premio"], "percentual", base_ok=cartoes_entregues > 0),
        _supervisor_c6_indicator("Nivel 4 acima de 300", nivel4, meta["nivel4_meta"], meta["nivel4_premio"], "inteiro", greater_than=True),
    ]
    snap["indicadores"] = indicadores
    snap["recebe_total"] = float(sum(float(item.get("Recebe_num", 0) or 0) for item in indicadores))
    snap["metas_batidas"] = int(sum(1 for item in indicadores if float(item.get("Recebe_num", 0) or 0) > 0))
    snap["qtd_indicadores"] = len(indicadores)
    snap["meta_mes"] = mkey
    return snap


def _criterio_score(txt: str, nome: str) -> int:
    if not isinstance(txt, str) or not txt.strip():
        return 0
    m = re.search(rf"{re.escape(nome)}\s*:\s*(\d+)", txt, flags=re.IGNORECASE)
    return int(m.group(1)) if m else 0


def _supervisor_snapshot_from_records(rows: List[dict], report_day: Optional[dt.date] = None) -> dict:
    month_start = month_first(report_day) if report_day else None
    if month_start:
        if month_start.month == 12:
            month_end = dt.date(month_start.year + 1, 1, 1) - dt.timedelta(days=1)
        else:
            month_end = dt.date(month_start.year, month_start.month + 1, 1) - dt.timedelta(days=1)
    else:
        month_end = None
    start = month_start or dt.date(2026, 4, 1)
    end = month_end or dt.date(2026, 4, 30)

    contas_abertas = 0
    contas_qualificadas = 0
    instalacoes_c6pay = 0
    c6pay_ativadas = 0
    pix_base = 0
    pix_cnpj = 0
    domicilio_qualificado = 0
    spending_qualificado = 0
    cartoes_entregues = 0
    cartoes_ativados = 0
    wallets_cadastradas = 0
    nivel4 = 0

    for row in rows:
        dt_abertura = _parse_br_date_text(row.get("dt_conta_criada"))
        dt_install = _parse_br_date_text(row.get("dt_install_maq"))
        dt_entrega = _parse_br_date_text(row.get("dt_entrega_cartao"))
        dt_cartao = _parse_br_date_text(row.get("dt_ativ_cartao_cred"))
        mes_ref = str(row.get("mes_ref_comiss", "") or "").strip().upper()
        pix_raw = _pix_clean_value(row.get("chaves_pix_forte", ""))
        wallet_raw = str(row.get("wallet", "") or "").strip().upper()
        criterios = str(row.get("criterios_atingidos_comiss", "") or "")
        level = _supervisor_level(row)
        tpv_m0 = float(pd.to_numeric(pd.Series([row.get("tpv_m0")]), errors="coerce").fillna(0.0).iloc[0])

        if dt_abertura and start <= dt_abertura <= end:
            contas_abertas += 1
        # Meta Supervisor segue a regra antiga:
        # cliente qualificado = nível 1+ no BY/critérios.
        if level >= 1:
            contas_qualificadas += 1
        if dt_install and month_start and month_end and month_start <= dt_install <= month_end:
            instalacoes_c6pay += 1
        if tpv_m0 > 1000:
            c6pay_ativadas += 1
        if mes_ref in {"M0", "M1", "M2"}:
            pix_base += 1
            if _pix_has_cnpj(pix_raw):
                pix_cnpj += 1
        if _criterio_score(criterios, "DOMICILIO") > 0:
            domicilio_qualificado += 1
        if _criterio_score(criterios, "SPENDING") > 0:
            spending_qualificado += 1
        cartao_entregue = bool(dt_entrega)
        if cartao_entregue:
            cartoes_entregues += 1
        if cartao_entregue and dt_cartao:
            cartoes_ativados += 1
        if _truthy_flag(wallet_raw):
            wallets_cadastradas += 1
        if level >= 4:
            nivel4 += 1

    pix_pct = (pix_cnpj / pix_base) if pix_base > 0 else 0.0
    wallet_pct = (wallets_cadastradas / cartoes_entregues) if cartoes_entregues > 0 else 0.0
    ativacao_cartao_pct = (cartoes_ativados / cartoes_entregues) if cartoes_entregues > 0 else 0.0
    qual_reward = _supervisor_reward_by_tier(
        contas_qualificadas, SUPERVISOR_C6_METAS["contas_qualificadas"]["faixas"]
    )
    qual_premio_referencia = 400.0
    if contas_qualificadas >= 1000:
        qual_premio_referencia = 1400.0
    elif contas_qualificadas >= 900:
        qual_premio_referencia = 540.0
    elif contas_qualificadas >= 800:
        qual_premio_referencia = 500.0

    indicadores = [
        {
            "Indicador": "Contas abertas",
            "Realizado_num": contas_abertas,
            "Meta_num": int(SUPERVISOR_C6_METAS["contas_abertas"]["meta"]),
            "Atingimento_num": (contas_abertas / SUPERVISOR_C6_METAS["contas_abertas"]["meta"]) if SUPERVISOR_C6_METAS["contas_abertas"]["meta"] > 0 else 0.0,
            "Premio_num": float(SUPERVISOR_C6_METAS["contas_abertas"]["premio"]),
            "Recebe_num": float(SUPERVISOR_C6_METAS["contas_abertas"]["premio"]) if contas_abertas >= SUPERVISOR_C6_METAS["contas_abertas"]["meta"] else 0.0,
            "Faixa": "",
            "Status": "Batida" if contas_abertas >= SUPERVISOR_C6_METAS["contas_abertas"]["meta"] else "Em aberto",
            "Tipo": "inteiro",
        },
        {
            "Indicador": "Contas qualificadas",
            "Realizado_num": contas_qualificadas,
            "Meta_num": 700,
            "Atingimento_num": (contas_qualificadas / 700.0) if 700 > 0 else 0.0,
            "Premio_num": qual_premio_referencia,
            "Recebe_num": qual_reward,
            "Faixa": "700 -> 400 | 800 -> 500 | 900 -> 540 | 1.000 -> 1.400",
            "Status": "Batida" if qual_reward > 0 else "Em aberto",
            "Tipo": "inteiro",
        },
        {
            "Indicador": "Instalacao C6 Pay",
            "Realizado_num": instalacoes_c6pay,
            "Meta_num": int(SUPERVISOR_C6_METAS["instalacao_c6pay"]["meta"]),
            "Atingimento_num": (instalacoes_c6pay / SUPERVISOR_C6_METAS["instalacao_c6pay"]["meta"]) if SUPERVISOR_C6_METAS["instalacao_c6pay"]["meta"] > 0 else 0.0,
            "Premio_num": float(SUPERVISOR_C6_METAS["instalacao_c6pay"]["premio"]),
            "Recebe_num": float(SUPERVISOR_C6_METAS["instalacao_c6pay"]["premio"]) if instalacoes_c6pay >= SUPERVISOR_C6_METAS["instalacao_c6pay"]["meta"] else 0.0,
            "Faixa": "",
            "Status": "Batida" if instalacoes_c6pay >= SUPERVISOR_C6_METAS["instalacao_c6pay"]["meta"] else "Em aberto",
            "Tipo": "inteiro",
        },
        {
            "Indicador": "C6 Pay ativada",
            "Realizado_num": c6pay_ativadas,
            "Meta_num": int(SUPERVISOR_C6_METAS["c6pay_ativada"]["meta"]),
            "Atingimento_num": (c6pay_ativadas / SUPERVISOR_C6_METAS["c6pay_ativada"]["meta"]) if SUPERVISOR_C6_METAS["c6pay_ativada"]["meta"] > 0 else 0.0,
            "Premio_num": float(SUPERVISOR_C6_METAS["c6pay_ativada"]["premio"]),
            "Recebe_num": float(SUPERVISOR_C6_METAS["c6pay_ativada"]["premio"]) if c6pay_ativadas >= SUPERVISOR_C6_METAS["c6pay_ativada"]["meta"] else 0.0,
            "Faixa": "",
            "Status": "Batida" if c6pay_ativadas >= SUPERVISOR_C6_METAS["c6pay_ativada"]["meta"] else "Em aberto",
            "Tipo": "inteiro",
        },
        {
            "Indicador": "Chave Pix CNPJ",
            "Realizado_num": pix_pct,
            "Meta_num": float(SUPERVISOR_C6_METAS["pix_cnpj"]["meta"]),
            "Atingimento_num": (pix_pct / SUPERVISOR_C6_METAS["pix_cnpj"]["meta"]) if SUPERVISOR_C6_METAS["pix_cnpj"]["meta"] > 0 else 0.0,
            "Premio_num": float(SUPERVISOR_C6_METAS["pix_cnpj"]["premio"]),
            "Recebe_num": float(SUPERVISOR_C6_METAS["pix_cnpj"]["premio"]) if pix_pct >= SUPERVISOR_C6_METAS["pix_cnpj"]["meta"] and pix_base > 0 else 0.0,
            "Faixa": "",
            "Status": "Batida" if pix_pct >= SUPERVISOR_C6_METAS["pix_cnpj"]["meta"] and pix_base > 0 else "Em aberto",
            "Tipo": "percentual",
        },
        {
            "Indicador": "Domicilio qualificado",
            "Realizado_num": domicilio_qualificado,
            "Meta_num": int(SUPERVISOR_C6_METAS["domicilio_qualificado"]["meta"]),
            "Atingimento_num": (domicilio_qualificado / SUPERVISOR_C6_METAS["domicilio_qualificado"]["meta"]) if SUPERVISOR_C6_METAS["domicilio_qualificado"]["meta"] > 0 else 0.0,
            "Premio_num": float(SUPERVISOR_C6_METAS["domicilio_qualificado"]["premio"]),
            "Recebe_num": float(SUPERVISOR_C6_METAS["domicilio_qualificado"]["premio"]) if domicilio_qualificado >= SUPERVISOR_C6_METAS["domicilio_qualificado"]["meta"] else 0.0,
            "Faixa": "",
            "Status": "Batida" if domicilio_qualificado >= SUPERVISOR_C6_METAS["domicilio_qualificado"]["meta"] else "Em aberto",
            "Tipo": "inteiro",
        },
        {
            "Indicador": "Spending qualificado",
            "Realizado_num": spending_qualificado,
            "Meta_num": int(SUPERVISOR_C6_METAS["spending_qualificado"]["meta"]),
            "Atingimento_num": (spending_qualificado / SUPERVISOR_C6_METAS["spending_qualificado"]["meta"]) if SUPERVISOR_C6_METAS["spending_qualificado"]["meta"] > 0 else 0.0,
            "Premio_num": float(SUPERVISOR_C6_METAS["spending_qualificado"]["premio"]),
            "Recebe_num": float(SUPERVISOR_C6_METAS["spending_qualificado"]["premio"]) if spending_qualificado >= SUPERVISOR_C6_METAS["spending_qualificado"]["meta"] else 0.0,
            "Faixa": "",
            "Status": "Batida" if spending_qualificado >= SUPERVISOR_C6_METAS["spending_qualificado"]["meta"] else "Em aberto",
            "Tipo": "inteiro",
        },
        {
            "Indicador": "Wallet",
            "Realizado_num": wallet_pct,
            "Meta_num": float(SUPERVISOR_C6_METAS["wallet"]["meta"]),
            "Atingimento_num": (wallet_pct / SUPERVISOR_C6_METAS["wallet"]["meta"]) if SUPERVISOR_C6_METAS["wallet"]["meta"] > 0 else 0.0,
            "Premio_num": float(SUPERVISOR_C6_METAS["wallet"]["premio"]),
            "Recebe_num": float(SUPERVISOR_C6_METAS["wallet"]["premio"]) if wallet_pct >= SUPERVISOR_C6_METAS["wallet"]["meta"] and cartoes_entregues > 0 else 0.0,
            "Faixa": "",
            "Status": "Batida" if wallet_pct >= SUPERVISOR_C6_METAS["wallet"]["meta"] and cartoes_entregues > 0 else ("Em aberto" if cartoes_entregues > 0 else "Sem base"),
            "Tipo": "percentual",
        },
        {
            "Indicador": "Ativacao cartao",
            "Realizado_num": ativacao_cartao_pct,
            "Meta_num": float(SUPERVISOR_C6_METAS["ativacao_cartao"]["meta"]),
            "Atingimento_num": (ativacao_cartao_pct / SUPERVISOR_C6_METAS["ativacao_cartao"]["meta"]) if SUPERVISOR_C6_METAS["ativacao_cartao"]["meta"] > 0 else 0.0,
            "Premio_num": float(SUPERVISOR_C6_METAS["ativacao_cartao"]["premio"]),
            "Recebe_num": float(SUPERVISOR_C6_METAS["ativacao_cartao"]["premio"]) if ativacao_cartao_pct >= SUPERVISOR_C6_METAS["ativacao_cartao"]["meta"] and cartoes_entregues > 0 else 0.0,
            "Faixa": "",
            "Status": "Batida" if ativacao_cartao_pct >= SUPERVISOR_C6_METAS["ativacao_cartao"]["meta"] and cartoes_entregues > 0 else "Em aberto",
            "Tipo": "percentual",
        },
        {
            "Indicador": "Nivel 4 acima de 300",
            "Realizado_num": nivel4,
            "Meta_num": int(SUPERVISOR_C6_METAS["nivel4"]["meta"]),
            "Atingimento_num": (nivel4 / SUPERVISOR_C6_METAS["nivel4"]["meta"]) if SUPERVISOR_C6_METAS["nivel4"]["meta"] > 0 else 0.0,
            "Premio_num": float(SUPERVISOR_C6_METAS["nivel4"]["premio"]),
            "Recebe_num": float(SUPERVISOR_C6_METAS["nivel4"]["premio"]) if nivel4 > SUPERVISOR_C6_METAS["nivel4"]["meta"] else 0.0,
            "Faixa": "",
            "Status": "Batida" if nivel4 > SUPERVISOR_C6_METAS["nivel4"]["meta"] else "Em aberto",
            "Tipo": "inteiro",
        },
    ]

    recebe_total = float(sum(item["Recebe_num"] for item in indicadores))
    metas_batidas = int(sum(1 for item in indicadores if float(item["Recebe_num"]) > 0))
    snapshot = {
        "report_day": fmt_date(report_day) if report_day else "",
        "recebe_total": recebe_total,
        "metas_batidas": metas_batidas,
        "qtd_indicadores": len(indicadores),
        "contas_abertas": contas_abertas,
        "contas_qualificadas": contas_qualificadas,
        "instalacoes_c6pay": instalacoes_c6pay,
        "c6pay_ativadas": c6pay_ativadas,
        "pix_pct": pix_pct,
        "pix_base": pix_base,
        "pix_cnpj": pix_cnpj,
        "domicilio_qualificado": domicilio_qualificado,
        "spending_qualificado": spending_qualificado,
        "cartoes_entregues": cartoes_entregues,
        "cartoes_ativados": cartoes_ativados,
        "wallets_cadastradas": wallets_cadastradas,
        "wallet_pct": wallet_pct,
        "ativacao_cartao_pct": ativacao_cartao_pct,
        "nivel4": nivel4,
        "indicadores": indicadores,
    }
    mkey = fmt_month(month_start) if month_start else "04/2026"
    return _supervisor_reapply_monthly_meta(snapshot, mkey)


def persist_supervisor_c6_daily(df_c6: pd.DataFrame):
    report_day = detect_report_day_from_df(df_c6)
    if report_day is None or COL_CNPJ not in df_c6.columns:
        return

    month_start = month_first(report_day)
    if month_start.month == 12:
        month_end = dt.date(month_start.year + 1, 1, 1) - dt.timedelta(days=1)
    else:
        month_end = dt.date(month_start.year, month_start.month + 1, 1) - dt.timedelta(days=1)
    aberturas_series = pd.to_datetime(df_c6.get(COL_ABERTURA), errors="coerce", dayfirst=True)
    contas_abertas_oficiais = int(
        (
            aberturas_series.notna()
            & (aberturas_series.dt.date >= month_start)
            & (aberturas_series.dt.date <= month_end)
        ).sum()
    )

    rows = []
    for _, row in df_c6.iterrows():
        cnpj = _normalize_cnpj_text(row.get(COL_CNPJ))
        if not cnpj:
            continue
        item = {
            "cnpj": cnpj,
            "tipo_pessoa": str(row.get("TIPO_PESSOA", "") or "").strip().upper(),
            "status_cc": str(row.get(COL_STATUS, "") or "").strip().upper(),
            "dt_conta_criada": fmt_date(pd.to_datetime(row.get(COL_ABERTURA), errors="coerce")),
            "mes_ref_comiss": str(row.get(COL_BR, "") or "").strip().upper(),
            "fl_qualificado_comiss": int(pd.to_numeric(pd.Series([row.get(COL_BY)]), errors="coerce").fillna(0).iloc[0]),
            "faixa_cash_in": int(pd.to_numeric(pd.Series([row.get("FAIXA_CASH_IN")]), errors="coerce").fillna(0).iloc[0]),
            "faixa_spending": int(pd.to_numeric(pd.Series([row.get("FAIXA_SPENDING")]), errors="coerce").fillna(0).iloc[0]),
            "criterios_atingidos_comiss": str(row.get(COL_CRIT, "") or ""),
            "chaves_pix_forte": str(row.get(COL_PIX, "") or "").strip().upper(),
            "status_pagamento_fatura": str(row.get("STATUS_PAGAMENTO_FATURA", "") or "").strip(),
            "tpv_m0": float(pd.to_numeric(pd.Series([row.get("TPV_M0")]), errors="coerce").fillna(0.0).iloc[0]),
            "tpv_m1": float(pd.to_numeric(pd.Series([row.get("TPV_M1")]), errors="coerce").fillna(0.0).iloc[0]),
            "tpv_m2": float(pd.to_numeric(pd.Series([row.get("TPV_M2")]), errors="coerce").fillna(0.0).iloc[0]),
            "dt_install_maq": fmt_date(pd.to_datetime(row.get("DT_INSTALL_MAQ"), errors="coerce")),
            "dt_ativacao_pay": fmt_date(pd.to_datetime(row.get("DT_ATIVACAO_PAY"), errors="coerce")),
            "dt_entrega_cartao": fmt_date(pd.to_datetime(row.get("DT_ENTREGA_CARTAO"), errors="coerce")),
            "dt_ativ_cartao_cred": fmt_date(pd.to_datetime(row.get("DT_ATIV_CARTAO_CRED"), errors="coerce")),
            "wallet": _wallet_raw_from_row(row),
        }
        tipo = item["tipo_pessoa"]
        status = item["status_cc"]
        if tipo != "PJ" or "MEI" in tipo or status in {"BLOQUEADA", "DESATIVADA", "ENCERRADA"}:
            continue
        rows.append(item)

    by_cnpj = {}
    for item in rows:
        by_cnpj[item["cnpj"]] = item

    snapshot = _supervisor_snapshot_from_records(list(by_cnpj.values()), report_day=report_day)
    snapshot["contas_abertas"] = contas_abertas_oficiais
    meta_mes = _supervisor_c6_meta_for_month(fmt_month(month_start))
    for indicador in snapshot.get("indicadores", []):
        if str(indicador.get("Indicador", "")).strip() == "Contas abertas":
            meta_ab = int(meta_mes["contas_abertas_meta"])
            indicador["Realizado_num"] = contas_abertas_oficiais
            indicador["Atingimento_num"] = (contas_abertas_oficiais / meta_ab) if meta_ab > 0 else 0.0
            indicador["Recebe_num"] = float(meta_mes["contas_abertas_premio"]) if contas_abertas_oficiais >= meta_ab else 0.0
            indicador["Status"] = "Batida" if contas_abertas_oficiais >= meta_ab else "Em aberto"
            break
    snapshot = _supervisor_reapply_monthly_meta(snapshot, fmt_month(month_start))
    snapshot["recebe_total"] = float(sum(float(item.get("Recebe_num", 0) or 0) for item in snapshot.get("indicadores", [])))
    snapshot["metas_batidas"] = int(sum(1 for item in snapshot.get("indicadores", []) if float(item.get("Recebe_num", 0) or 0) > 0))
    store = safe_json_load(HIST_SUPERVISOR_C6_DAILY, default={}) or {}
    store[fmt_date(report_day)] = snapshot
    safe_json_save(HIST_SUPERVISOR_C6_DAILY, store)


def _format_supervisor_indicator_view(df_supervisor: pd.DataFrame) -> pd.DataFrame:
    view = df_supervisor.copy()
    def _premio_display(row):
        return br_money(float(row.get("Premio_num", 0) or 0))

    view["Realizado"] = view.apply(
        lambda row: f'{float(row["Realizado_num"]) * 100:.1f}%'.replace(".", ",")
        if row["Tipo"] == "percentual"
        else br_int(int(row["Realizado_num"])),
        axis=1,
    )
    view["Meta"] = view.apply(
        lambda row: f'{float(row["Meta_num"]) * 100:.1f}%'.replace(".", ",")
        if row["Tipo"] == "percentual"
        else br_int(int(row["Meta_num"])),
        axis=1,
    )
    view["Atingimento"] = view["Atingimento_num"].apply(lambda x: f"{float(x) * 100:.1f}%".replace(".", ","))
    view["Prêmio"] = view["Premio_num"].apply(br_money)
    view["Recebe"] = view["Recebe_num"].apply(br_money)
    return view[["Indicador", "Realizado", "Meta", "Atingimento", "Prêmio", "Recebe", "Status"]]


def _format_supervisor_indicator_view(df_supervisor: pd.DataFrame) -> pd.DataFrame:
    view = df_supervisor.copy()
    def _premio_display(row):
        return br_money(float(row.get("Premio_num", 0) or 0))

    view["Realizado"] = view.apply(
        lambda row: f'{float(row["Realizado_num"]) * 100:.1f}%'.replace(".", ",")
        if row["Tipo"] == "percentual"
        else br_int(int(row["Realizado_num"])),
        axis=1,
    )
    view["Meta"] = view.apply(
        lambda row: f'{float(row["Meta_num"]) * 100:.1f}%'.replace(".", ",")
        if row["Tipo"] == "percentual"
        else br_int(int(row["Meta_num"])),
        axis=1,
    )
    view["Atingimento"] = view["Atingimento_num"].apply(lambda x: f"{float(x) * 100:.1f}%".replace(".", ","))
    view["Premio"] = view.apply(_premio_display, axis=1)
    view["Recebe"] = view["Recebe_num"].apply(br_money)
    view = view.rename(columns={"Premio": "Prêmio"})
    return view[["Indicador", "Realizado", "Meta", "Atingimento", "Prêmio", "Recebe", "Status"]]


def _supervisor_daily_evolution_df(store: dict) -> pd.DataFrame:
    rows = []
    prev = None
    for day in sorted(store.keys(), key=lambda x: dt.datetime.strptime(x, "%d/%m/%Y")):
        item = store.get(day) or {}
        rows.append({
            "Data": day,
            "Contas abertas": int(item.get("contas_abertas", 0)),
            "Contas qualificadas": int(item.get("contas_qualificadas", 0)),
            "C6 Pay ativadas": int(item.get("c6pay_ativadas", 0)),
            "Wallet cadastrada": int(item.get("wallets_cadastradas", 0) or 0),
            "Wallet %": float(item.get("wallet_pct", 0.0) or 0.0),
            "PIX CNPJ %": float(item.get("pix_pct", 0.0)),
            "Domicílio qualificado": int(item.get("domicilio_qualificado", 0)),
            "Spending qualificado": int(item.get("spending_qualificado", 0)),
            "Ativação cartão %": float(item.get("ativacao_cartao_pct", 0.0)),
            "Recebimento potencial": float(item.get("recebe_total", 0.0)),
            "Delta potencial": None if prev is None else float(item.get("recebe_total", 0.0)) - float(prev.get("recebe_total", 0.0)),
        })
        prev = item
    return pd.DataFrame(rows)


def _supervisor_daily_evolution_df(store: dict) -> pd.DataFrame:
    rows = []
    prev = None
    for day in sorted(store.keys(), key=lambda x: dt.datetime.strptime(x, "%d/%m/%Y")):
        item = store.get(day) or {}
        rows.append({
            "Data": day,
            "Contas abertas": int(item.get("contas_abertas", 0)),
            "Contas qualificadas": int(item.get("contas_qualificadas", 0)),
            "C6 Pay ativadas": int(item.get("c6pay_ativadas", 0)),
            "PIX CNPJ %": float(item.get("pix_pct", 0.0)),
            "Domicílio qualificado": int(item.get("domicilio_qualificado", 0)),
            "Spending qualificado": int(item.get("spending_qualificado", 0)),
            "Ativação cartão %": float(item.get("ativacao_cartao_pct", 0.0)),
            "Recebimento potencial": float(item.get("recebe_total", 0.0)),
            "Delta potencial": None if prev is None else float(item.get("recebe_total", 0.0)) - float(prev.get("recebe_total", 0.0)),
        })
        prev = item
    return pd.DataFrame(rows)


def _supervisor_month_from_day_key(day_key: str) -> str:
    try:
        d = dt.datetime.strptime(str(day_key), "%d/%m/%Y").date()
        return fmt_month(d)
    except Exception:
        return ""


def _supervisor_openings_from_history_month(mkey: str, fallback: int = 0) -> int:
    hist = safe_json_load(HIST_OPEN_DAILY, default={}) or {}
    total = 0
    found = False
    for day_key, qty in hist.items():
        try:
            d = dt.datetime.strptime(str(day_key), "%d/%m/%Y").date()
        except Exception:
            continue
        if fmt_month(d) == mkey:
            total += int(qty or 0)
            found = True
    if found:
        return int(total)
    try:
        total_visao = int(_visao_month_openings_count(mkey))
        if total_visao > 0:
            return total_visao
    except Exception:
        pass
    return int(fallback or 0)


def _supervisor_monthly_history_df(store: dict) -> pd.DataFrame:
    rows = []
    months = sorted(
        {m for m in (_supervisor_month_from_day_key(k) for k in store.keys()) if m},
        key=month_key_str,
    )
    for mkey in months:
        if month_key_str(mkey) < month_key_str("04/2026"):
            continue
        month_items = [(k, v) for k, v in store.items() if _supervisor_month_from_day_key(k) == mkey]
        if not month_items:
            continue
        latest_day, item = sorted(month_items, key=lambda kv: dt.datetime.strptime(kv[0], "%d/%m/%Y"))[-1]
        contas_abertas_hist = _supervisor_openings_from_history_month(mkey, int(item.get("contas_abertas", 0) or 0))
        rows.append({
            "Mês": mkey,
            "Última data-base": latest_day,
            "Recebimento potencial": float(item.get("recebe_total", 0.0) or 0.0),
            "Metas batidas": int(item.get("metas_batidas", 0) or 0),
            "Indicadores": int(item.get("qtd_indicadores", 0) or 0),
            "Contas abertas": contas_abertas_hist,
            "Contas qualificadas": int(item.get("contas_qualificadas", 0) or 0),
            "C6 Pay ativadas": int(item.get("c6pay_ativadas", 0) or 0),
            "Wallet cadastrada": int(item.get("wallets_cadastradas", 0) or 0),
            "Wallet %": float(item.get("wallet_pct", 0.0) or 0.0),
            "PIX CNPJ %": float(item.get("pix_pct", 0.0) or 0.0),
            "Ativação cartão %": float(item.get("ativacao_cartao_pct", 0.0) or 0.0),
        })
    return pd.DataFrame(rows)


def _supervisor_pdf_bytes(report_day: str, summary: dict, view_supervisor: pd.DataFrame, view_daily: pd.DataFrame) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image

    bio = io.BytesIO()
    doc = SimpleDocTemplate(bio, pagesize=landscape(A4), leftMargin=10 * mm, rightMargin=10 * mm, topMargin=10 * mm, bottomMargin=10 * mm)
    styles = getSampleStyleSheet()
    title_style = styles["Title"]
    title_style.textColor = colors.HexColor("#10233f")
    subtitle_style = styles["Heading2"]
    subtitle_style.textColor = colors.HexColor("#1f3f67")
    note_style = styles["BodyText"]
    note_style.textColor = colors.HexColor("#44556b")

    story = []
    logo_path = os.path.join(os.getcwd(), "LOGO CORRETA.png")
    header_left = [
        Paragraph("Meta do Supervisor de C6 Empresas", title_style),
        Paragraph(f"Relatório diário - data base {report_day or '-'}", styles["Normal"]),
        Paragraph("<b>Uso interno e confidencial.</b> Material restrito da Assis e Mollerke.", note_style),
    ]
    header_right = []
    if os.path.exists(logo_path):
        header_right.append(Image(logo_path, width=34 * mm, height=18 * mm))
    header_tbl = Table([[header_left, header_right or [""]]], colWidths=[220 * mm, 50 * mm])
    header_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.extend([
        header_tbl,
        Spacer(1, 6),
        Paragraph(
            f"Recebimento potencial: <b>{br_money(float(summary.get('recebe_total', 0.0)))}</b> | "
            f"Metas batidas: <b>{br_int(int(summary.get('metas_batidas', 0)))} / {br_int(int(summary.get('qtd_indicadores', 0)))}</b> | "
            f"Contas abertas: <b>{br_int(int(summary.get('contas_abertas', 0)))}</b> | "
            f"Contas qualificadas: <b>{br_int(int(summary.get('contas_qualificadas', 0)))}</b>",
            styles["BodyText"],
        ),
        Spacer(1, 6),
    ])

    def _tbl(df: pd.DataFrame):
        data = [list(df.columns)] + df.fillna("").astype(str).values.tolist()
        tbl = Table(data, repeatRows=1)
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#10233f")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#d7dee8")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f6f9fc")]),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
            ("TOPPADDING", (0, 0), (-1, 0), 6),
            ("BOTTOMPADDING", (0, 1), (-1, -1), 3),
            ("TOPPADDING", (0, 1), (-1, -1), 3),
        ]))
        return tbl

    story.append(Paragraph("Resumo da meta", styles["Heading3"]))
    story.append(_tbl(view_supervisor))
    story.append(Spacer(1, 8))
    if not view_daily.empty:
        story.append(Paragraph("Evolução diária", styles["Heading3"]))
        story.append(_tbl(view_daily.tail(5)))

    doc.build(story)
    bio.seek(0)
    return bio.getvalue()


def _supervisor_pdf_bytes(report_day: str, summary: dict, view_supervisor: pd.DataFrame, view_daily: pd.DataFrame) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image

    bio = io.BytesIO()
    doc = SimpleDocTemplate(bio, pagesize=landscape(A4), leftMargin=10 * mm, rightMargin=10 * mm, topMargin=10 * mm, bottomMargin=10 * mm)
    styles = getSampleStyleSheet()
    title_style = styles["Title"]
    title_style.textColor = colors.HexColor("#173858")
    title_style.fontSize = 18
    subtitle_style = styles["Heading2"]
    subtitle_style.textColor = colors.HexColor("#2d577f")
    subtitle_style.fontSize = 11
    note_style = styles["BodyText"]
    note_style.textColor = colors.HexColor("#4c6077")
    note_style.fontSize = 8.5

    story = []
    logo_path = os.path.join(os.getcwd(), "LOGO CORRETA.png")
    header_left = [
        Paragraph("Meta do Supervisor de C6 Empresas", title_style),
        Paragraph(f"Relatório diário - data base {report_day or '-'}", subtitle_style),
    ]
    header_right = []
    if os.path.exists(logo_path):
        header_right.append(Image(logo_path, width=34 * mm, height=18 * mm))
    header_tbl = Table([[header_left, header_right or [""]]], colWidths=[220 * mm, 50 * mm])
    header_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.extend([
        header_tbl,
        Spacer(1, 6),
        Paragraph(
            f"Recebimento potencial: <b>{br_money(float(summary.get('recebe_total', 0.0)))}</b> | "
            f"Metas batidas: <b>{br_int(int(summary.get('metas_batidas', 0)))} / {br_int(int(summary.get('qtd_indicadores', 0)))}</b> | "
            f"Contas abertas: <b>{br_int(int(summary.get('contas_abertas', 0)))}</b>",
            styles["BodyText"],
        ),
        Spacer(1, 6),
    ])

    def _tbl(df: pd.DataFrame):
        data = [list(df.columns)] + df.fillna("").astype(str).values.tolist()
        tbl = Table(data, repeatRows=1)
        tbl.hAlign = "CENTER"
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#173a5e")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#d3dde8")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f8fb")]),
            ("FONTSIZE", (0, 0), (-1, -1), 7.0),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("ALIGN", (0, 0), (0, -1), "LEFT"),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 5),
            ("TOPPADDING", (0, 0), (-1, 0), 5),
            ("BOTTOMPADDING", (0, 1), (-1, -1), 3),
            ("TOPPADDING", (0, 1), (-1, -1), 3),
        ]))
        return tbl

    story.append(Paragraph("Resumo da meta", styles["Heading3"]))
    story.append(_tbl(view_supervisor))
    story.append(Spacer(1, 6))
    if not view_daily.empty:
        story.append(Paragraph("Evolução diária", styles["Heading3"]))
        story.append(_tbl(view_daily.tail(5)))

    def _draw_footer(canvas, doc_obj):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#4c6077"))
        footer_text = "Uso interno e confidencial. Material restrito da Assis e Mollerke."
        x = doc_obj.leftMargin
        y = 7 * mm
        canvas.drawString(x, y, footer_text)
        canvas.restoreState()

    doc.build(story, onFirstPage=_draw_footer, onLaterPages=_draw_footer)
    bio.seek(0)
    return bio.getvalue()


def _supervisor_email_filename(report_day: str) -> str:
    safe_day = str(report_day or dt.date.today().strftime("%d/%m/%Y")).replace("/", "-")
    return f"Relatorio_Confidencial_Meta_Supervisor_C6_Empresas_Data_Base_{safe_day}.pdf"


def _supervisor_email_subject(report_day: str) -> str:
    return f"Confidencial | Meta Supervisor C6 Empresas | Data-base {report_day or '-'}"


def _supervisor_email_body(report_day: str) -> str:
    return (
        "Prezados,\n\n"
        f"Segue em anexo o relatório diário confidencial da Meta do Supervisor de C6 Empresas, com data-base {report_day or '-'}.\n\n"
        "O material é de uso interno e confidencial, destinado exclusivamente aos responsáveis autorizados.\n\n"
        "Atenciosamente,\n"
        "Assis e Mollerke"
    )


def _email_subject_for_reports(report_day: str, labels: List[str]) -> str:
    base = " | ".join(labels) if labels else "Relatórios C6"
    return f"Confidencial | {base} | Data-base {report_day or '-'}"


def _email_body_for_reports(report_day: str, labels: List[str]) -> str:
    lista = "\n".join([f"- {label}" for label in labels]) if labels else "- Relatórios selecionados"
    return (
        "Prezados,\n\n"
        f"Segue em anexo o envio confidencial dos relatórios com data-base {report_day or '-'}.\n\n"
        "Relatórios enviados:\n"
        f"{lista}\n\n"
        "O material é de uso interno e confidencial, destinado exclusivamente aos responsáveis autorizados.\n\n"
        "Atenciosamente,\n"
        "Assis e Mollerke"
    )


def _load_supervisor_email_cfg() -> dict:
    cfg = safe_json_load(SUPERVISOR_C6_EMAIL_CFG, default={}) or {}
    if not isinstance(cfg, dict):
        cfg = {}
    return {
        "to_email": str(cfg.get("to_email") or SMTP_DEFAULT_TO).strip(),
        "smtp_password": str(cfg.get("smtp_password") or SMTP_DEFAULT_PASSWORD).strip(),
    }


def _save_supervisor_email_cfg(to_email: str, smtp_password: str):
    safe_json_save(
        SUPERVISOR_C6_EMAIL_CFG,
        {
            "to_email": str(to_email or "").strip(),
            "smtp_password": str(smtp_password or "").strip(),
        },
    )


def _smtp_password_from_secrets() -> str:
    if "email" in st.secrets:
        return str((st.secrets.get("email") or {}).get("password") or "").strip()
    return ""


def send_supervisor_email(to_email: str, smtp_password: str, subject: str, body: str, pdf_name: str, pdf_bytes: bytes):
    msg = EmailMessage()
    msg["From"] = SMTP_SENDER
    msg["To"] = to_email
    msg["Cc"] = SMTP_SENDER
    msg["Subject"] = subject
    msg.set_content(body)
    msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=pdf_name)

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.login(SMTP_SENDER, smtp_password)
        server.send_message(msg)


def send_email_with_attachments(to_email: str, smtp_password: str, subject: str, body: str, attachments: List[dict]):
    from email.utils import formatdate
    msg = EmailMessage()
    msg["From"] = SMTP_SENDER
    msg["To"] = to_email
    msg["Cc"] = SMTP_SENDER
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body)
    for att in attachments:
        data = att.get("data")
        if not isinstance(data, (bytes, bytearray)) or not data:
            continue
        maintype = str(att.get("maintype") or "application")
        subtype = str(att.get("subtype") or "octet-stream")
        filename = str(att.get("filename") or "anexo.bin")
        msg.add_attachment(bytes(data), maintype=maintype, subtype=subtype, filename=filename)

    passwords = []
    raw_pwd = str(smtp_password or "")
    if raw_pwd:
        passwords.append(raw_pwd)
        trimmed_pwd = raw_pwd.rstrip(".")
        if trimmed_pwd and trimmed_pwd != raw_pwd:
            passwords.append(trimmed_pwd)

    attempts = [
        ("ssl", SMTP_PORT),
        ("starttls", SMTP_PORT_TLS),
    ]
    last_err = None
    for pwd_try in passwords:
        for mode, port in attempts:
            try:
                if mode == "ssl":
                    with smtplib.SMTP_SSL(SMTP_HOST, port, timeout=30) as server:
                        server.login(SMTP_SENDER, pwd_try)
                        server.send_message(msg)
                    return
                with smtplib.SMTP(SMTP_HOST, port, timeout=30) as server:
                    server.ehlo()
                    server.starttls()
                    server.ehlo()
                    server.login(SMTP_SENDER, pwd_try)
                    server.send_message(msg)
                return
            except Exception as e:
                last_err = e
    if last_err:
        raise last_err


def _report_pdf_bytes(title: str, report_day: str, df: pd.DataFrame) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image

    bio = io.BytesIO()
    doc = SimpleDocTemplate(bio, pagesize=landscape(A4), leftMargin=10 * mm, rightMargin=10 * mm, topMargin=10 * mm, bottomMargin=10 * mm)
    styles = getSampleStyleSheet()
    title_style = styles["Title"]
    title_style.textColor = colors.HexColor("#173858")
    title_style.fontSize = 18
    subtitle_style = styles["Heading2"]
    subtitle_style.textColor = colors.HexColor("#2d577f")
    subtitle_style.fontSize = 11
    section_style = styles["Heading3"]
    section_style.textColor = colors.HexColor("#173858")
    section_style.fontSize = 11

    logo_path = os.path.join(os.getcwd(), "LOGO CORRETA.png")
    story = []
    header_left = [
        Paragraph(title, title_style),
        Paragraph(f"Relatório diário - data base {report_day or '-'}", subtitle_style),
    ]
    header_right = []
    if os.path.exists(logo_path):
        header_right.append(Image(logo_path, width=34 * mm, height=18 * mm))
    header_tbl = Table([[header_left, header_right or [""]]], colWidths=[220 * mm, 50 * mm])
    header_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.extend([header_tbl, Spacer(1, 6)])

    view = df.copy()
    if "operador" in view.columns:
        view = view[view["operador"].astype(str).str.strip().ne("")].copy()

    def _norm_header_name(value: str) -> str:
        txt = str(value or "").strip().lower()
        txt = unicodedata.normalize("NFKD", txt).encode("ASCII", "ignore").decode("ASCII")
        txt = re.sub(r"[^a-z0-9%]+", "_", txt)
        txt = re.sub(r"_+", "_", txt).strip("_")
        return txt

    def _fmt_col_value(col_name: str, value):
        if value is None or pd.isna(value):
            return ""
        name = _norm_header_name(col_name)
        if "comissao" in name or name.startswith("valor") or "_valor" in name:
            try:
                return br_money(float(value))
            except Exception:
                return str(value)
        if "%" in str(col_name or "") or "eficiencia" in name or "atingimento" in name:
            try:
                return f"{float(value):.1f}%".replace(".", ",")
            except Exception:
                return str(value)
        if "operador" in name:
            return str(value)
        try:
            if isinstance(value, (int, float)) and float(value).is_integer():
                return br_int(int(value))
        except Exception:
            pass
        return str(value)

    if not view.empty:
        display = view.copy()
        for col in display.columns:
            display[col] = display[col].apply(lambda x, c=col: _fmt_col_value(c, x))
    else:
        display = view.copy()

    resumo_cards = []
    if not view.empty and "operador" in view.columns:
        resumo_cards.append(["Operadores", br_int(int(view["operador"].nunique()))])
    if not view.empty:
        norm_cols = {_norm_header_name(c): c for c in view.columns}

        if "comissao" in norm_cols:
            col = norm_cols["comissao"]
            resumo_cards.append(["Comissão total", br_money(float(pd.to_numeric(view[col], errors="coerce").fillna(0).sum()))])
        elif "valor" in norm_cols:
            col = norm_cols["valor"]
            resumo_cards.append(["Valor total", br_money(float(pd.to_numeric(view[col], errors="coerce").fillna(0).sum()))])

        if "indicados" in norm_cols:
            col = norm_cols["indicados"]
            resumo_cards.append(["Clientes indicados", br_int(int(pd.to_numeric(view[col], errors="coerce").fillna(0).sum()))])
        elif "clientes" in norm_cols:
            col = norm_cols["clientes"]
            resumo_cards.append(["Clientes", br_int(int(pd.to_numeric(view[col], errors="coerce").fillna(0).sum()))])

        if "contas_validas" in norm_cols:
            col = norm_cols["contas_validas"]
            resumo_cards.append(["Contas válidas", br_int(int(pd.to_numeric(view[col], errors="coerce").fillna(0).sum()))])
        elif "qualificados" in norm_cols:
            col = norm_cols["qualificados"]
            resumo_cards.append(["Qualificados", br_int(int(pd.to_numeric(view[col], errors="coerce").fillna(0).sum()))])
        elif "abertas_14d" in norm_cols:
            col = norm_cols["abertas_14d"]
            resumo_cards.append(["Abertas em 14 dias", br_int(int(pd.to_numeric(view[col], errors="coerce").fillna(0).sum()))])

    if resumo_cards:
        story.extend([Paragraph("Resumo executivo", section_style), Spacer(1, 4)])
        cards_row = [[Paragraph(f"<b>{label}</b><br/>{value}", styles["BodyText"]) for label, value in resumo_cards[:4]]]
        card_tbl = Table(cards_row, colWidths=[67 * mm] * len(cards_row[0]))
        card_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f4f8fb")),
            ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#d3dde8")),
            ("INNERGRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#d3dde8")),
            ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#173858")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]))
        story.extend([card_tbl, Spacer(1, 8)])

    story.extend([Paragraph("Equipe", section_style), Spacer(1, 4)])

    if len(display) > 18:
        display = display.head(18).copy()

    data = [list(display.columns)] + display.fillna("").astype(str).values.tolist()
    col_count = max(len(display.columns), 1)
    usable_width = doc.width
    if col_count == 1:
        col_widths = [usable_width]
    else:
        first_col = min(80 * mm, usable_width * 0.32)
        other_w = max((usable_width - first_col) / max(col_count - 1, 1), 24 * mm)
        col_widths = [first_col] + [other_w] * (col_count - 1)
    tbl = Table(data, repeatRows=1, colWidths=col_widths)
    tbl.hAlign = "CENTER"
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#173a5e")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#d3dde8")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f8fb")]),
        ("FONTSIZE", (0, 0), (-1, -1), 8.0),
        ("LEADING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("TOPPADDING", (0, 0), (-1, 0), 6),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 4),
        ("TOPPADDING", (0, 1), (-1, -1), 4),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (0, -1), "LEFT"),
        ("LEFTPADDING", (0, 0), (0, -1), 8),
    ]))
    story.append(tbl)

    def _draw_footer(canvas, doc_obj):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#4c6077"))
        canvas.drawString(doc_obj.leftMargin, 7 * mm, "Uso interno e confidencial. Material restrito da Assis e Mollerke.")
        canvas.restoreState()

    doc.build(story, onFirstPage=_draw_footer, onLaterPages=_draw_footer)
    bio.seek(0)
    return bio.getvalue()


def _operator_pdf_view(report_key: str, df: pd.DataFrame) -> pd.DataFrame:
    base = df.copy()
    key = str(report_key or "").strip().lower()

    if key == "act":
        cols = [
            ("operador", "Operador"),
            ("clientes_indicados", "Indicados"),
            ("contas_abertas", "Contas abertas"),
            ("abertas_14d", "Abertas 14d"),
            ("eficiencia_%", "Eficiência %"),
            ("comissao_total", "Comissão"),
        ]
    elif key == "act_conversao":
        cols = [
            ("operador", "Operador"),
            ("clientes_act", "Clientes ACT"),
            ("clientes_abriram_conta", "Abriram conta"),
            ("clientes_abriram_14d", "Abertas 14d"),
            ("conversao_%", "Conversão %"),
        ]
    elif key == "oco":
        cols = [
            ("operador", "Operador"),
            ("clientes_trabalhados", "Clientes"),
            ("contas_validas", "Contas válidas"),
            ("indicados_mes_base", "Indicados mês"),
            ("eficiencia_vs_indicados_%", "Eficiência %"),
            ("comissao_total", "Comissão"),
        ]
    elif key == "oql":
        if "valor_real_total" in base.columns or "valor_teorico_total" in base.columns:
            real = pd.to_numeric(base.get("valor_real_total", 0), errors="coerce").fillna(0.0)
            teorico = pd.to_numeric(base.get("valor_teorico_total", 0), errors="coerce").fillna(0.0)
            base["valor_pdf_total"] = [rv if rv > 0 else tv for rv, tv in zip(real.tolist(), teorico.tolist())]
        cols = [
            ("operador", "Operador"),
            ("clientes_base", "Clientes"),
            ("qualificados", "Qualificados"),
            ("nivel4", "Nível 4"),
            ("eficiencia_qualificacao_%", "Eficiência %"),
            ("valor_pdf_total", "Valor"),
        ]
    else:
        cols = [(c, c) for c in list(base.columns)[:6]]

    keep = [c for c, _ in cols if c in base.columns]
    view = base[keep].copy() if keep else pd.DataFrame()
    rename_map = {c: lbl for c, lbl in cols if c in view.columns}
    view = view.rename(columns=rename_map)
    return view


def compute_supervisor_c6_meta(selected_month: Optional[str] = None) -> Tuple[pd.DataFrame, pd.DataFrame, dict]:
    store = safe_json_load(HIST_SUPERVISOR_C6_DAILY, default={}) or {}
    df_visao_atual = st.session_state.get("c6_daily_visao_df")
    if df_visao_atual is None or getattr(df_visao_atual, "empty", True):
        df_visao_atual, _, _ = _load_daily_import_cache("visao")
    if df_visao_atual is not None and not getattr(df_visao_atual, "empty", True):
        try:
            persist_supervisor_c6_daily(df_visao_atual)
            store = safe_json_load(HIST_SUPERVISOR_C6_DAILY, default={}) or {}
        except Exception:
            if not store:
                store = {}
    if not store:
        return pd.DataFrame(), pd.DataFrame(), {}

    applied_store = {}
    for day_key, item in store.items():
        mkey = _supervisor_month_from_day_key(day_key)
        snap = _supervisor_reapply_monthly_meta(item or {}, mkey) if mkey else (item or {})
        if mkey and month_key_str(mkey) >= month_key_str("04/2026"):
            snap["contas_abertas"] = _supervisor_openings_from_history_month(mkey, int(snap.get("contas_abertas", 0) or 0))
            snap = _supervisor_reapply_monthly_meta(snap, mkey)
        applied_store[day_key] = snap

    candidate_days = list(applied_store.keys())
    if selected_month:
        month_days = [d for d in candidate_days if _supervisor_month_from_day_key(d) == selected_month]
        if month_days:
            candidate_days = month_days
    latest_day = sorted(candidate_days, key=lambda x: dt.datetime.strptime(x, "%d/%m/%Y"))[-1]
    snapshot = applied_store.get(latest_day) or {}
    indicators = pd.DataFrame(snapshot.get("indicadores", []))
    daily_store = {
        k: v for k, v in applied_store.items()
        if not selected_month or _supervisor_month_from_day_key(k) == selected_month
    }
    daily_df = _supervisor_daily_evolution_df(daily_store)
    snapshot["history_store"] = applied_store
    snapshot["monthly_history"] = _supervisor_monthly_history_df(applied_store)
    snapshot["report_day"] = latest_day
    return indicators, daily_df, snapshot


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


def _old_paid_max_before(limit_month_key: str) -> Dict[str, float]:
    month_levels = safe_json_load(HIST_MONTH_LEVELS, default={}) or {}
    months = sorted(list(month_levels.keys()), key=month_key_str)
    paid_max: Dict[str, float] = {}

    for mkey in months:
        if month_key_str(mkey) >= month_key_str(limit_month_key):
            break

        cmap: Dict[str, int] = month_levels.get(mkey, {}) or {}
        cmap = {k: int(v) for k, v in cmap.items() if str(k).strip() != ""}
        qtd_qual = len(cmap)

        if mkey == "12/2025":
            _, precos = FAIXAS[-1][1], FAIXAS[-1][2]
        else:
            _, precos = faixa_por_qtd(qtd_qual)

        for cnpj, lvl in cmap.items():
            cheio = float(precos.get(int(lvl), 0.0))
            prev = float(paid_max.get(cnpj, 0.0))
            paid_max[cnpj] = max(prev, cheio)

    return paid_max


def _c6_visao_row_eligible_pj(row: dict) -> bool:
    """PJ sem MEI e sem conta bloqueada/desativada/encerrada — alinhado ao comparativo regra antiga no painel."""
    tipo = str(row.get("tipo_pessoa", "") or "").upper()
    status = str(row.get("status_cc", "") or "").upper()
    if tipo != "PJ":
        return False
    if "MEI" in tipo:
        return False
    if status in {"BLOQUEADA", "DESATIVADA", "ENCERRADA"}:
        return False
    return True


def _visao_month_valid_rows(mkey: str) -> Dict[str, dict]:
    cmap = _visao_month_rows(mkey)
    valid: Dict[str, dict] = {}
    for cnpj, row in cmap.items():
        if not _c6_visao_row_eligible_pj(row):
            continue
        valid[str(cnpj)] = row
    return valid


def _visao_month_old_rule_levels(mkey: str) -> Dict[str, int]:
    levels: Dict[str, int] = {}
    for cnpj, row in _visao_month_valid_rows(mkey).items():
        lvl = int(_supervisor_level(row))
        if lvl >= 1:
            levels[str(cnpj)] = lvl
    return levels


def _visao_month_openings_count(mkey: str) -> int:
    start, end = _month_range(mkey)
    if not start or not end:
        return 0
    total = 0
    for _, row in _visao_month_rows(mkey).items():
        try:
            abertura = dt.datetime.strptime(str(row.get("dt_conta_criada", "") or ""), "%d/%m/%Y").date()
        except Exception:
            continue
        if start <= abertura <= end:
            total += 1
    return total


def _refresh_current_month_remuneration_from_rows(mkey: str, month_rows: Dict[str, dict]):
    valid_rows = [(str(cnpj), row) for cnpj, row in (month_rows or {}).items() if _c6_visao_row_eligible_pj(row)]

    old_paid_before = _old_paid_max_before(mkey)
    old_levels = {cnpj: int(_supervisor_level(row)) for cnpj, row in valid_rows if int(_supervisor_level(row)) >= 1}
    qtd_old = len(old_levels)
    faixa_nome, precos = faixa_por_qtd(qtd_old) if mkey != "12/2025" else (FAIXAS[-1][1], FAIXAS[-1][2])
    lvl_counts = {1: 0, 2: 0, 3: 0, 4: 0}
    old_cheio = 0.0
    old_receber = 0.0
    for cnpj, row in valid_rows:
        nivel = int(_supervisor_level(row))
        if nivel < 1:
            continue
        if nivel in lvl_counts:
            lvl_counts[nivel] += 1
        bank_old = _old_bank_cartilha_values(row)
        if bank_old is not None:
            cheio, _, receber = bank_old
        else:
            cheio = float(precos.get(nivel, 0.0))
            ja_pago = _old_prior_paid_value(old_paid_before, cnpj, mkey)
            receber = max(0.0, cheio - ja_pago)
        old_cheio += cheio
        old_receber += receber
    old_summary = {
        "faixa": faixa_nome,
        "qualificadas": qtd_old,
        "n1": lvl_counts[1],
        "n2": lvl_counts[2],
        "n3": lvl_counts[3],
        "n4": lvl_counts[4],
        "deveria_receber": old_cheio,
        "ja_pago_ref": max(0.0, old_cheio - old_receber),
        "receber_mes": old_receber,
    }
    old_store = safe_json_load(HIST_RESUMO_MENSAL, default={}) or {}
    old_store[mkey] = old_summary
    safe_json_save(HIST_RESUMO_MENSAL, old_store)
    _patch_panel_cache_row(PANEL_C6_INCREMENTAL_CACHE, mkey, [
        mkey, faixa_nome, qtd_old, lvl_counts[1], lvl_counts[2], lvl_counts[3], lvl_counts[4],
        old_summary["deveria_receber"], old_summary["ja_pago_ref"], old_summary["receber_mes"],
    ])

    use_bank_values = _nova_month_uses_bank_values(valid_rows)
    qtd_new = 0
    tmp = {}
    fator_pref = float(_nova_cartilha_fator_por_qualificadas(0))
    for cnpj, row in valid_rows:
        bank_vals = _nova_bank_cartilha_values(row) if use_bank_values else None
        if bank_vals is not None:
            best0 = bank_vals[0]
        else:
            best0 = max(
                _nova_cashin_amount(float(row.get("cash_in_valor", 0.0) or 0.0), fator_pref),
                _nova_spending_amount(float(row.get("spending_total_mtd", 0.0) or 0.0), fator_pref),
                _nova_tpv_amount(_nova_tpv_for_cartilha(row), fator_pref),
            )
        if best0 > 0:
            qtd_new += 1
        tmp[cnpj] = row

    fator_mes = float(_nova_cartilha_fator_por_qualificadas(qtd_new))
    paid_max: Dict[str, dict] = dict(_nova_cartilha_paid_max_start([mkey]))
    detail_counts = {"cash_in": 0, "spending": 0, "c6pay": 0, "pix_cnpj": 0, "wallet": 0}
    new_cheio = 0.0
    new_receber = 0.0
    c6pay_credenciados = 0
    for cnpj, row in tmp.items():
        cash_amt = _nova_cashin_amount(float(row.get("cash_in_valor", 0.0) or 0.0), fator_mes)
        spending_amt = _nova_spending_amount(float(row.get("spending_total_mtd", 0.0) or 0.0), fator_mes)
        tpv_amt = _nova_tpv_amount(_nova_tpv_for_cartilha(row), fator_mes)
        best_amt = max(cash_amt, spending_amt, tpv_amt)
        bank_vals = _nova_bank_cartilha_values(row) if use_bank_values else None
        if bank_vals is not None:
            best_amt, bank_receive = bank_vals
        else:
            bank_receive = None
        if best_amt > 0:
            if best_amt == tpv_amt:
                detail_counts["c6pay"] += 1
            elif best_amt == spending_amt:
                detail_counts["spending"] += 1
            else:
                detail_counts["cash_in"] += 1
        cnpjx = _normalize_cnpj_text(cnpj)
        prev = _nova_prior_paid_value(paid_max, cnpjx, mkey)
        diff = max(0.0, best_amt - prev) if bank_receive is None else max(0.0, bank_receive)
        pix_bonus = 0.0
        if bank_receive is None and mkey == "06/2026" and best_amt > 0 and _pix_has_cnpj(row.get("chaves_pix_forte", "")):
            pix_bonus = 15.0
        best_total = best_amt + pix_bonus
        diff_total = diff + pix_bonus
        new_cheio += best_total
        new_receber += diff_total
        _nova_paid_update(paid_max, cnpjx, mkey, best_total)
        if pix_bonus > 0:
            detail_counts["pix_cnpj"] += 1
        dt_abertura = _parse_br_date_text(row.get("dt_conta_criada"))
        dt_install = _parse_br_date_text(row.get("dt_install_maq"))
        if dt_abertura and dt_install and fmt_month(dt_abertura) == mkey:
            c6pay_credenciados += 1
        if _truthy_flag(row.get("wallet")):
            detail_counts["wallet"] += 1

    new_summary = {
        "qualificadas": qtd_new,
        "acelerador": fator_mes,
        "cash_in": detail_counts["cash_in"],
        "spending": detail_counts["spending"],
        "c6pay": detail_counts["c6pay"],
        "c6pay_credenciamento": c6pay_credenciados,
        "pix_cnpj": detail_counts["pix_cnpj"],
        "wallet": detail_counts["wallet"],
        "deveria_receber": new_cheio,
        "ja_pago_ref": max(0.0, new_cheio - new_receber),
        "receber_mes": new_receber,
    }
    new_store = safe_json_load(HIST_NOVA_RESUMO_MENSAL, default={}) or {}
    new_store[mkey] = new_summary
    safe_json_save(HIST_NOVA_RESUMO_MENSAL, new_store)
    safe_json_save(HIST_NOVA_PAGO_POR_CNPJ, paid_max)
    _patch_panel_cache_row(PANEL_C6_CARTILHA_NOVA_CACHE, mkey, [
        mkey, qtd_new, fator_mes, detail_counts["cash_in"], detail_counts["spending"],
        detail_counts["c6pay"], c6pay_credenciados, detail_counts["pix_cnpj"], detail_counts["wallet"],
        new_summary["deveria_receber"], new_summary["ja_pago_ref"], new_summary["receber_mes"],
    ])


def _visao_df_openings_count(df_visao: pd.DataFrame, report_month: Optional[dt.date] = None) -> int:
    if df_visao is None or df_visao.empty or COL_ABERTURA not in df_visao.columns:
        return 0
    abertura = pd.to_datetime(df_visao[COL_ABERTURA], errors="coerce", dayfirst=True)
    if report_month is not None:
        abertura = abertura[(abertura.dt.year == report_month.year) & (abertura.dt.month == report_month.month)]
    return int(abertura.notna().sum())


def _old_rule_receber_from_visao_df(df_c6: pd.DataFrame, all_rows: bool = False) -> float:
    if df_c6 is None or df_c6.empty:
        return 0.0

    mes_rel = detect_report_month_from_df(df_c6)
    if mes_rel is None:
        return 0.0

    mkey = fmt_month(mes_rel)
    paid_max = _old_paid_max_before(mkey)

    df_calc = df_c6.copy() if all_rows else _panel_c6_valid_df(df_c6).copy()
    if df_calc.empty:
        return 0.0

    if COL_CNPJ not in df_calc.columns:
        return 0.0

    df_calc["_cnpj"] = _normalize_cnpj_series(df_calc[COL_CNPJ])
    df_calc = df_calc[df_calc["_cnpj"] != ""].copy()
    if df_calc.empty:
        return 0.0

    if "PREVISAO_COMISS" in df_calc.columns:
        previsao = pd.to_numeric(df_calc["PREVISAO_COMISS"], errors="coerce").fillna(0.0)
        return float(previsao.sum())

    df_calc["_nivel"] = parse_level(df_calc)
    cmap = (
        df_calc.groupby("_cnpj")["_nivel"]
        .max()
        .reset_index()
    )
    cmap = {
        str(r["_cnpj"]): int(r["_nivel"])
        for _, r in cmap.iterrows()
        if int(r["_nivel"]) >= 1
    }
    if not cmap:
        return 0.0

    qtd_qual = len(cmap)
    if mkey == "12/2025":
        _, precos = FAIXAS[-1][1], FAIXAS[-1][2]
    else:
        _, precos = faixa_por_qtd(qtd_qual)

    total_receber = 0.0
    for cnpj, lvl in cmap.items():
        cheio = float(precos.get(int(lvl), 0.0))
        prev = _old_prior_paid_value(paid_max, cnpj, mkey)
        diff = cheio - prev
        if diff < 0:
            diff = 0.0
        total_receber += diff
    return float(total_receber)


def _panel_c6_valid_df(df_c6: pd.DataFrame) -> pd.DataFrame:
    df = df_c6.copy()
    tipo_col = "TIPO_PESSOA" if "TIPO_PESSOA" in df.columns else None
    status_col = COL_STATUS if COL_STATUS in df.columns else None

    tipo_s = normalize_str(df[tipo_col]).str.upper() if tipo_col else pd.Series(["PJ"] * len(df), index=df.index)
    status_s = normalize_str(df[status_col]).str.upper() if status_col else pd.Series([""] * len(df), index=df.index)

    mask = (
        (tipo_s == "PJ")
        & (~tipo_s.str.contains("MEI", na=False))
        & (~status_s.isin(["BLOQUEADA", "DESATIVADA", "ENCERRADA"]))
    )
    return df[mask].copy()


def _refresh_compare_pending_from_daily_c6(df_c6: Optional[pd.DataFrame], pending: dict) -> dict:
    if df_c6 is None or df_c6.empty:
        return pending

    df = df_c6.copy()

    if COL_ABERTURA not in df.columns:
        df[COL_ABERTURA] = pd.NaT
    if COL_CASHIN_MTD not in df.columns:
        df[COL_CASHIN_MTD] = 0.0
    if COL_BR not in df.columns:
        df[COL_BR] = ""
    if COL_PIX not in df.columns:
        df[COL_PIX] = ""

    df[COL_ABERTURA] = to_date_series(df[COL_ABERTURA])
    df[COL_CASHIN_MTD] = pd.to_numeric(df[COL_CASHIN_MTD], errors="coerce").fillna(0.0)
    df[COL_BR] = normalize_str(df[COL_BR]).str.upper()

    opened_counts = (
        df[df[COL_ABERTURA].notna()]
        .assign(_d=df[COL_ABERTURA])
        .query("_d >= @HIST_START")
        .groupby("_d")
        .size()
        .to_dict()
    )
    opened_counts = {fmt_date(k): int(v) for k, v in opened_counts.items()}
    if opened_counts:
        daily_upsert_many(HIST_OPEN_DAILY, opened_counts)

    cmp_day = detect_report_day_from_df(df)
    if not cmp_day or cmp_day < HIST_START:
        return pending

    mes_rel = detect_report_month_from_df(df)
    cmp_mes_ref = fmt_month(mes_rel) if mes_rel else ""

    dfq_tmp = df.copy()
    dfq_tmp["_nivel"] = parse_level(dfq_tmp)
    qmask = dfq_tmp["_nivel"] >= 1
    br_tmp = normalize_str(dfq_tmp.get(COL_BR, pd.Series([""] * len(dfq_tmp), index=dfq_tmp.index))).str.upper()

    s_pix = df.get(COL_PIX, pd.Series([""] * len(df), index=df.index)).apply(_pix_clean_value)
    has_pix = s_pix.apply(_pix_is_valid)

    day_key = fmt_date(cmp_day)
    rec = pending.get(day_key, {})
    rec.update({
        "mes_ref": cmp_mes_ref,
        "c6_total": int(len(df)),
        "qual_total": int(qmask.sum()),
        "qual_m0": int((qmask & (br_tmp == "M0")).sum()),
        "qual_m1": int((qmask & (br_tmp == "M1")).sum()),
        "qual_m2": int((qmask & (br_tmp == "M2")).sum()),
        "pix_total": int(has_pix.sum()),
        "cashin_total": float(df[COL_CASHIN_MTD].sum()),
        "base_receber_mes": float(_old_rule_receber_from_visao_df(df, all_rows=True)),
    })
    pending[day_key] = rec
    return pending


def _refresh_compare_pending_from_daily_leads(df_leads: Optional[pd.DataFrame], pending: dict) -> dict:
    if df_leads is None or df_leads.empty:
        return pending

    df = df_leads.copy()
    if COL_LEADS_DATA not in df.columns:
        cand = [c for c in df.columns if "CADAST" in str(c).upper() and "DATA" in str(c).upper()]
        if cand:
            df[COL_LEADS_DATA] = df[cand[0]]
        elif len(df.columns) >= 13:
            df[COL_LEADS_DATA] = df.iloc[:, 12]
        else:
            df[COL_LEADS_DATA] = pd.NA

    df[COL_LEADS_DATA] = to_date_series(df[COL_LEADS_DATA])

    leads_counts = (
        df[df[COL_LEADS_DATA].notna()]
        .assign(_d=df[COL_LEADS_DATA])
        .query("_d >= @HIST_START")
        .groupby("_d")
        .size()
        .to_dict()
    )
    leads_counts = {fmt_date(k): int(v) for k, v in leads_counts.items()}
    if leads_counts:
        daily_upsert_many(HIST_LEADS_DAILY, leads_counts)

    cmp_day = detect_report_day_from_df(df)
    if not cmp_day or cmp_day < HIST_START:
        return pending

    day_key = fmt_date(cmp_day)
    rec = pending.get(day_key, {})
    if not rec.get("mes_ref"):
        rec["mes_ref"] = fmt_month(dt.date(cmp_day.year, cmp_day.month, 1))
    rec["leads_total"] = int(len(df))
    pending[day_key] = rec
    return pending


# =========================================================
# RECOMPUTE INCREMENTAL (SEM CRIAR MESES)
# =========================================================
def recompute_incremental() -> pd.DataFrame:
    month_levels = safe_json_load(HIST_MONTH_LEVELS, default={}) or {}
    months = sorted(set(list(month_levels.keys()) + list((_load_visao_month_snapshot() or {}).keys())), key=month_key_str)

    paid_max: Dict[str, float] = {}
    resumo: Dict[str, dict] = {}

    rows = []
    for mkey in months:
        if _visao_month_rows(mkey):
            cmap = _visao_month_old_rule_levels(mkey)
        else:
            cmap = month_levels.get(mkey, {}) or {}
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

        valid_rows = _visao_month_valid_rows(mkey) if _visao_month_rows(mkey) else {}
        use_bank_old = any(_old_bank_cartilha_values(row) is not None for row in valid_rows.values())
        if use_bank_old:
            for cnpj, row in valid_rows.items():
                bank_old = _old_bank_cartilha_values(row)
                if bank_old is None:
                    continue
                cheio, _, receber = bank_old
                total_cheio += cheio
                total_receber += receber
                if cheio > 0:
                    paid_max[str(cnpj)] = max(float(paid_max.get(str(cnpj), 0.0)), cheio)
            ja_pago_ref = max(0.0, total_cheio - total_receber)
        else:
            for cnpj, lvl in cmap.items():
                cheio = float(precos.get(int(lvl), 0.0))
                prev = _old_prior_paid_value(paid_max, cnpj, mkey)
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
    st.session_state["logged_in"] = True
    st.session_state["user_role"] = "admin"
    st.session_state["auth_user"] = "admin"
    st.session_state["operator_filter"] = ""
    return True

    url = str(getattr(st.context, "url", "") or "")
    if "localhost" in url or "127.0.0.1" in url:
        st.session_state["logged_in"] = True
        st.session_state["user_role"] = "admin"
        st.session_state["auth_user"] = "admin"
        st.session_state["operator_filter"] = ""
        return True
    st.sidebar.markdown(
        """
        <div class="am-login-brand">
            <div class="am-login-kicker">ASSIS E MOLLERKE</div>
            <div class="am-login-title">Acesso restrito</div>
            <div class="am-login-copy">Aplicação desenvolvida pela Assis e Mollerke para uso interno. Não compartilhar.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.sidebar.markdown("### Acesso")
    u = st.sidebar.text_input("Usuário", value="")
    p = st.sidebar.text_input("Senha", value="", type="password")
    if st.sidebar.button("Entrar"):
        users_cfg = st.secrets.get("users", {})
        admin_pwd = str(users_cfg.get("admin", "123456"))
        supervisor_pwd = str(users_cfg.get("supervisor", "supervisor2026"))
        operador_pwd = str(users_cfg.get("operador", "operador2026"))

        logged = False
        role = ""
        operator_filter = ""
        auth_user = str(u or "").strip()
        if auth_user == "admin" and p == admin_pwd:
            logged = True
            role = "admin"
        elif auth_user == "supervisor" and p == supervisor_pwd:
            logged = True
            role = "supervisor"
        elif auth_user and p == operador_pwd:
            logged = True
            role = "operador"
            operator_filter = _normalize_person_key(auth_user)

        st.session_state["logged_in"] = logged
        if logged:
            st.session_state["user_role"] = role
            st.session_state["auth_user"] = auth_user
            st.session_state["operator_filter"] = operator_filter
        else:
            st.sidebar.error("Usuário ou senha inválidos.")
    return st.session_state.get("logged_in", False)


def apply_theme():
    st.markdown(
        """
        <style>
            :root {
                --am-ink: #0f2742;
                --am-ink-soft: #5c6f86;
                --am-navy: #123253;
                --am-blue: #285f9f;
                --am-blue-soft: #e8f1fb;
                --am-bg: #f4f7fb;
                --am-panel: #ffffff;
                --am-line: #d7e1ec;
                --am-positive: #117a43;
                --am-positive-bg: #e7f6ee;
                --am-negative: #b42318;
                --am-negative-bg: #fdecec;
                --am-shadow: 0 8px 22px rgba(15,39,66,0.06);
            }
            .stApp {
                background: linear-gradient(180deg, #fbfcfe 0%, #f4f7fb 100%);
                color: var(--am-ink);
            }
            .block-container {
                max-width: 1380px;
                padding-top: 0.85rem;
                padding-bottom: 3rem;
            }
            div[data-testid="stVerticalBlock"] {
                gap: 0.85rem;
            }
            section[data-testid="stSidebar"]{
                background: linear-gradient(180deg, #0f2742 0%, #143757 100%);
                border-right: 1px solid rgba(255,255,255,0.08);
            }
            section[data-testid="stSidebar"] * {
                color: #ffffff !important;
            }
            section[data-testid="stSidebar"] .stButton button {
                background: rgba(255,255,255,0.08);
                border: 1px solid rgba(255,255,255,0.18);
                color: white !important;
                border-radius: 14px;
                font-weight: 700;
                min-height: 46px;
            }
            section[data-testid="stSidebar"] .stTextInput input {
                background: rgba(255,255,255,0.98) !important;
                color: var(--am-ink) !important;
                border-radius: 14px !important;
                border: 1px solid rgba(255,255,255,0.30) !important;
            }
            .am-login-brand {
                margin: 0 0 18px 0;
                padding: 16px;
                border-radius: 18px;
                background: rgba(255,255,255,0.08);
                border: 1px solid rgba(255,255,255,0.10);
            }
            .am-login-kicker {
                font-size: 11px;
                font-weight: 800;
                letter-spacing: 0.18em;
                margin-bottom: 8px;
                opacity: 0.82;
            }
            .am-login-title {
                font-size: 18px;
                line-height: 1.1;
                font-weight: 700;
                margin-bottom: 10px;
            }
            .am-login-copy {
                font-size: 12px;
                line-height: 1.5;
                opacity: 0.92;
            }
            div[data-testid="stMetric"]{
                background: var(--am-panel);
                border:1px solid var(--am-line);
                border-radius:10px;
                padding:13px 15px;
                box-shadow: var(--am-shadow);
                transition: all 0.2s ease;
            }
            div[data-testid="stMetric"]:hover {
                box-shadow:0 10px 22px rgba(16,35,63,0.08);
            }
            div[data-testid="stMetricLabel"] {
                color: var(--am-ink-soft) !important;
                font-weight: 600 !important;
                font-size: 0.84rem !important;
            }
            div[data-testid="stMetricValue"] {
                color: var(--am-ink) !important;
                font-weight: 650 !important;
                letter-spacing: 0;
                font-size: clamp(1.55rem, 1.7vw, 2.15rem) !important;
                line-height: 1.05 !important;
            }
            h1, h2, h3{
                color: var(--am-ink);
                font-weight: 700;
                letter-spacing: 0;
            }
            p, label, .stCaption {
                color: var(--am-ink-soft);
            }
            .stCaption {
                font-size: 0.78rem;
                opacity: 0.82;
            }
            .am-badge-ok{
                display:inline-block; padding:6px 16px; border-radius:999px;
                background:var(--am-positive-bg); color:var(--am-positive);
                font-weight:700; font-size:13px; border:1px solid rgba(17,122,67,0.15);
            }
            .am-badge-bad{
                display:inline-block; padding:6px 16px; border-radius:999px;
                background:var(--am-negative-bg); color:var(--am-negative);
                font-weight:700; font-size:13px; border:1px solid rgba(180,35,24,0.14);
            }
            div[data-testid="stDataFrame"] {
                border: 1px solid var(--am-line);
                border-radius: 10px;
                overflow: hidden;
                box-shadow: 0 6px 18px rgba(16,35,63,0.04);
                background: white;
            }
            div[data-testid="stDataFrame"] [role="columnheader"] {
                background: #f3f6fa !important;
                color: var(--am-ink) !important;
                font-weight: 700 !important;
                border-bottom: 1px solid var(--am-line) !important;
            }
            div[data-testid="stDataFrame"] [role="gridcell"],
            div[data-testid="stDataFrame"] [role="columnheader"] {
                font-size: 13px !important;
            }
            .stFileUploader > div {
                border: 1.5px dashed #c9d6e6;
                border-radius: 10px;
                padding: 20px;
                background: #fcfdff;
            }
            .stTabs [data-baseweb="tab-list"] {
                gap: 8px;
                background: rgba(255,255,255,0.92);
                padding: 8px;
                border-radius: 10px;
                border: 1px solid var(--am-line);
            }
            .stTabs [data-baseweb="tab"] {
                height: 44px;
                padding: 0 16px;
                border-radius: 8px;
                color: var(--am-ink-soft);
                font-weight: 650;
            }
            .stTabs [aria-selected="true"] {
                background: var(--am-blue-soft) !important;
                border: 1px solid #c9d7ea !important;
                box-shadow: none;
                color: var(--am-ink) !important;
            }
            .stTabs [aria-selected="true"] p {
                color: var(--am-ink) !important;
            }
            .stButton button, .stDownloadButton button {
                border-radius: 8px !important;
                border: 1px solid var(--am-line) !important;
                font-weight: 700 !important;
                min-height: 42px;
                box-shadow: none;
                background: #f8fbff !important;
                color: #173a5f !important;
            }
            .stDownloadButton button {
                background: #eef5fc !important;
                border-color: #bfd1e5 !important;
            }
            .stButton button[kind="primary"], .stDownloadButton button[kind="primary"] {
                background: var(--am-navy) !important;
                color: white !important;
                border: none !important;
            }
            .stTextInput input, .stSelectbox [data-baseweb="select"] > div, .stTextArea textarea {
                border-radius: 8px !important;
                border: 1px solid var(--am-line) !important;
                background: rgba(255,255,255,0.98) !important;
            }
            div[data-testid="stAlert"] {
                border-radius: 18px;
                border: 1px solid var(--am-line);
            }
            div[data-testid="stExpander"] {
                border: 1px solid var(--am-line);
                border-radius: 10px;
                overflow: hidden;
                background: linear-gradient(180deg, rgba(255,255,255,0.97), rgba(247,250,255,0.97));
            }
            div[data-testid="stExpander"] summary {
                font-weight: 800;
                color: var(--am-ink);
            }
            .am-hero-box {
                border-radius: 14px;
                padding: 24px 26px;
                background: linear-gradient(135deg, #0f2742 0%, #173d63 100%);
                color: white;
                box-shadow: 0 18px 30px rgba(15,36,67,0.12);
            }
            .am-hero-kicker {
                display:inline-block;
                padding: 6px 12px;
                border-radius: 999px;
                border: 1px solid rgba(255,255,255,0.18);
                background: rgba(255,255,255,0.08);
                font-size: 11px;
                font-weight: 800;
                letter-spacing: 0.14em;
                margin-bottom: 14px;
            }
            .am-hero-title {
                font-size: 32px;
                font-weight: 800;
                line-height: 1.04;
                letter-spacing: 0;
                margin-bottom: 10px;
            }
            .am-hero-copy {
                font-size: 14px;
                line-height: 1.55;
                color: rgba(255,255,255,0.92);
                max-width: 760px;
            }
            .am-hero-notice {
                margin-top: 16px;
                display: inline-block;
                padding: 10px 14px;
                border-radius: 14px;
                background: rgba(255,255,255,0.10);
                border: 1px solid rgba(255,255,255,0.14);
                font-size: 13px;
                font-weight: 700;
            }
            hr {
                margin: 2rem 0;
                border: none;
                height: 2px;
                background: linear-gradient(90deg, transparent, #d4dfea, transparent);
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


def show_logo_and_title():
    logo_path = os.path.join(APP_DIR, "LOGO CORRETA.png")

    c1, c2 = st.columns([1.1, 5.9], vertical_alignment="center")
    with c1:
        if os.path.exists(logo_path):
            st.image(logo_path, width=150)
    with c2:
        st.markdown(
            """
            <div class="am-hero-box">
                <div class="am-hero-kicker">ASSIS E MOLLERKE</div>
                <div class="am-hero-title">Painel estratégico corporativo</div>
                <div class="am-hero-copy">
                    Aplicação desenvolvida pela Assis e Mollerke para acompanhamento executivo, operação comercial e monitoramento do desempenho do escritório.
                </div>
                <div class="am-hero-notice">Uso interno e confidencial. Este aplicativo não deve ser compartilhado.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )


_ORIGINAL_ST_DATAFRAME = st.dataframe


def _am_dataframe(data=None, *args, **kwargs):
    if "height" not in kwargs:
        rows = None
        try:
            rows = int(len(data))
        except Exception:
            rows = None
        if rows is None:
            kwargs["height"] = 420
        else:
            kwargs["height"] = max(220, min(520, 72 + rows * 34))
    return _ORIGINAL_ST_DATAFRAME(data, *args, **kwargs)


st.dataframe = _am_dataframe


def reset_all_data():
    for p in [
        HIST_OPEN_DAILY, HIST_LEADS_DAILY, HIST_MONTH_LEVELS,
        HIST_PAGO_POR_CNPJ, HIST_RESUMO_MENSAL, HIST_SNAPSHOT_MENSAL,
        HIST_NOVA_PAGO_POR_CNPJ, HIST_NOVA_RESUMO_MENSAL,
        HIST_SUPERVISOR_C6_DAILY,
        HIST_COMPARE_DAILY,
        LEADS_STATUS_DAILY_PATH,
        LEADS_CONTROL_PATH,
        C6_LEADS_CNPJ_TRACK,
    ]:
        safe_json_delete(p)
    local_json_delete(HIST_VISAO_MENSAL)


# =========================================================
# C6 OPERAÇÃO — NOVO MÓDULO
# =========================================================
C6_OP_IMPORT_LOG = os.path.join(DATA_DIR, "c6_op_importacoes.json")
C6_OP_PIX_TRACK = os.path.join(DATA_DIR, "c6_op_pix_track.json")
C6_OP_OMC_MAXPAY = os.path.join(DATA_DIR, "c6_op_omc_maxpay.json")
C6_OP_REMUN_CFG = os.path.join(DATA_DIR, "c6_op_remuneracao_config.json")
C6_OP_REMUN_HISTORY = os.path.join(DATA_DIR, "c6_op_remuneracao_historico.json")

def _coalesce_col(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    cols_map = {str(c).strip().upper(): c for c in df.columns}
    for cand in candidates:
        key = str(cand).strip().upper()
        if key in cols_map:
            return cols_map[key]
    return None

def _normalize_cnpj_value(v) -> str:
    return re.sub(r"\D", "", "" if v is None or pd.isna(v) else str(v))

def _normalize_cnpj_series(s: pd.Series) -> pd.Series:
    return s.astype("string").fillna("").apply(_normalize_cnpj_value)


def _normalize_pix_text(v) -> str:
    txt = str(v or "").strip().upper()
    if txt == "-":
        return ""
    return txt


def _pix_clean_value(v) -> str:
    txt = _normalize_pix_text(v)
    txt = txt.replace(" ", "")
    txt = txt.replace("'", "")
    return txt


def _pix_is_valid(v) -> bool:
    return _pix_clean_value(v) in PIX_VALID_VALUES


def _pix_has_cnpj(v) -> bool:
    return _pix_clean_value(v) in {"CNPJ", "CNPJ|PHONE", "CNPJ|EMAIL", "CNPJ|EMAIL|PHONE"}


def _normalize_status_key(v) -> str:
    txt = str(v or "").strip().upper()
    txt = unicodedata.normalize("NFKD", txt).encode("ASCII", "ignore").decode("ASCII")
    txt = re.sub(r"\s+", " ", txt)
    return txt

def _read_ops_file(uploaded_file) -> pd.DataFrame:
    raw = uploaded_file.getvalue()
    name = uploaded_file.name.lower()

    def _fix_shifted_resumo_operadores(df: pd.DataFrame) -> pd.DataFrame:
        cols = [str(c) for c in df.columns]
        if not {"Nome", "CPF / CNPJ", "Agente"}.issubset(set(cols)):
            return df
        first_vals = df["Nome"].dropna().astype(str).head(20)
        if first_vals.empty or first_vals.str.replace(r"\D+", "", regex=True).str.len().lt(11).all():
            return df
        fixed = pd.DataFrame(index=df.index)
        fixed["Nome"] = ""
        fixed["Cód"] = df["Cód"] if "Cód" in df.columns else ""
        fixed["CPF / CNPJ"] = df["Nome"]
        fixed["Agente Flag"] = ""
        fixed["Agente"] = df["CPF / CNPJ"]
        fixed["Ação"] = df["Agente"]
        fixed["Data"] = df["Unnamed: 4"] if "Unnamed: 4" in df.columns else ""
        fixed["Hora"] = df["Ação"] if "Ação" in df.columns else ""
        fixed["Histórico"] = df["Data"] if "Data" in df.columns else ""
        fixed["Fila"] = df["Hora"] if "Hora" in df.columns else ""
        fixed["Fone Discado"] = df["Histórico"] if "Histórico" in df.columns else ""
        fixed["Credor"] = df["Fone Discado"] if "Fone Discado" in df.columns else ""
        fixed["Atraso"] = df["Credor"] if "Credor" in df.columns else ""
        fixed["Valor"] = df["Atraso"] if "Atraso" in df.columns else ""
        fixed["Inclusão"] = df["Valor"] if "Valor" in df.columns else ""
        fixed["CDEC"] = df["Inclusão"] if "Inclusão" in df.columns else ""
        fixed["Fase"] = df["Fase"] if "Fase" in df.columns else ""
        return fixed

    if name.endswith(".csv"):
        try:
            text = raw.decode("utf-8-sig", errors="replace")
            lines = text.splitlines()
            if lines:
                header_parts = lines[0].split(";")
                first_row_parts = lines[1].split(";") if len(lines) > 1 else []
                if len(header_parts) == 16 and len(first_row_parts) == 17:
                    fixed_cols = [
                        "Nome", "Cód", "CPF / CNPJ", "Agente Flag", "Agente", "Ação", "Data", "Hora",
                        "Histórico", "Fila", "Fone Discado", "Credor", "Atraso", "Valor", "Inclusão", "CDEC", "Fase"
                    ]
                    rows = []
                    for ln in lines[1:]:
                        parts = ln.split(";")
                        if len(parts) < 17:
                            parts = parts + ([""] * (17 - len(parts)))
                        elif len(parts) > 17:
                            parts = parts[:16] + [";".join(parts[16:])]
                        rows.append(parts)
                    return _fix_shifted_resumo_operadores(pd.DataFrame(rows, columns=fixed_cols, dtype=str))
        except Exception:
            pass
        last_err = None
        for enc in ["utf-8-sig", "latin1", "cp1252"]:
            for sep in [";", ",", "\t", "|"]:
                try:
                    df = pd.read_csv(io.BytesIO(raw), sep=sep, dtype=str, encoding=enc)
                    if len(df.columns) > 1:
                        return _fix_shifted_resumo_operadores(df)
                except Exception as e:
                    last_err = e
        raise last_err
    return _fix_shifted_resumo_operadores(pd.read_excel(io.BytesIO(raw), dtype=str, engine="openpyxl"))

def _days_between(start_ts, end_ts) -> Optional[int]:
    if pd.isna(start_ts) or pd.isna(end_ts):
        return None
    return int((pd.Timestamp(end_ts).normalize() - pd.Timestamp(start_ts).normalize()).days)

def _business_days_between(start_ts, end_ts) -> Optional[int]:
    if pd.isna(start_ts) or pd.isna(end_ts):
        return None
    s = pd.Timestamp(start_ts).date()
    e = pd.Timestamp(end_ts).date()
    if e < s:
        return 0
    return int(pd.bdate_range(s, e).size)

def _faixa_abertura(days: Optional[int]) -> str:
    if days is None:
        return ""
    if days <= 1:
        return "1 dia"
    if 2 <= days <= 5:
        return "2 a 5 dias"
    if 6 <= days <= 10:
        return "6 a 10 dias"
    if 11 <= days <= 12:
        return "11 a 12 dias"
    if 13 <= days <= 14:
        return "13 a 14 dias"
    return "15+ dias"

def _faixa_idade_empresa(dt_fund, data_base) -> str:
    if pd.isna(dt_fund) or pd.isna(data_base):
        return ""
    fund = pd.Timestamp(dt_fund)
    base = pd.Timestamp(data_base)
    months = max(0, (base.year - fund.year) * 12 + (base.month - fund.month))
    if months <= 3:
        return "1 a 3 meses"
    if months <= 6:
        return "4 a 6 meses"
    if months <= 9:
        return "7 a 9 meses"
    if months <= 12:
        return "10 a 12 meses"
    if months <= 18:
        return "13 a 18 meses"
    if months <= 24:
        return "19 a 24 meses"
    if months <= 36:
        return "25 a 36 meses"
    if months <= 48:
        return "37 a 48 meses"
    if months <= 60:
        return "49 a 60 meses"
    return "61+ meses"

def _stage_from_open_date(open_dt, base_dt) -> str:
    if pd.isna(open_dt) or pd.isna(base_dt):
        return ""
    o = pd.Timestamp(open_dt)
    b = pd.Timestamp(base_dt)
    m0 = o.strftime("%Y-%m")
    base = b.strftime("%Y-%m")
    m1 = (o.to_period("M") + 1).strftime("%Y-%m")
    m2 = (o.to_period("M") + 2).strftime("%Y-%m")
    if base == m0:
        return "M0"
    if base == m1:
        return "M1"
    if base == m2:
        return "M2"
    return ""

def _extract_ops_base(df_ops: pd.DataFrame) -> pd.DataFrame:
    df = df_ops.copy()
    cnpj_col = _coalesce_col(df, ["CPF / CNPJ", "CNPJ", "CPF/CNPJ", "CD_CPF_CNPJ_CLIENTE"])
    nome_col = _coalesce_col(df, ["CLIENTE", "NOME_CLIENTE", "Nome", "NOME"])
    operador_col = _coalesce_col(df, ["Agente", "Unnamed: 4", "OPERADOR", "Nome do operador", "NOME_OPERADOR"])
    historico_col = _coalesce_col(df, ["Histórico", "Historico"])
    acao_code_col = _coalesce_col(df, ["Ação", "ACAO", "ACAO_GRUPO", "GRUPO_ACAO", "TIPO_ACAO"])
    acao_desc_col = _coalesce_col(df, ["Ação", "ACAO"])
    data_col = _coalesce_col(df, ["Data", "DATA", "Data ação operador"])
    hora_col = _coalesce_col(df, ["Hora", "HORA"])
    inclusao_col = _coalesce_col(df, ["Inclusão", "INCLUSAO", "Inclusão cliente"])
    abertura_col = _coalesce_col(df, ["DATA_ABERTURA_CNPJ", "Data abertura CNPJ"])

    out = pd.DataFrame()
    out["cnpj"] = _normalize_cnpj_series(df[cnpj_col]) if cnpj_col else ""
    out["nome_cliente"] = normalize_str(df[nome_col]) if nome_col else ""
    operador_base = normalize_str(df[operador_col]) if operador_col else pd.Series([""] * len(df), dtype="string")
    if historico_col:
        historico = df[historico_col].astype("string").fillna("")
        op_hist = historico.str.extract(r"AGENTE:\s*([^\r\n<]+)", expand=False).astype("string").fillna("").str.strip()
        operador_base = operador_base.where(~operador_base.str.upper().isin(["", "0", "1", "SISTEMA"]), op_hist)
    out["operador"] = operador_base
    acao_code = normalize_str(df[acao_code_col]).str.upper() if acao_code_col else pd.Series([""] * len(df), dtype="string")
    acao_desc = normalize_str(df[acao_desc_col]).str.upper() if acao_desc_col else pd.Series([""] * len(df), dtype="string")
    acao_mix = (acao_code.fillna("") + " " + acao_desc.fillna("")).str.upper().str.strip()

    def _grp(x: str) -> str:
        if not x:
            return ""
        if "ACT" in x or "INDIC" in x:
            return "ACT"
        if "OQL" in x or "OMC" in x or "QUAL" in x or "PIX" in x or "PAY" in x or "WALLET" in x:
            return "OQL"
        if "OCO" in x or "OAB" in x or "ABR" in x:
            return "OCO"
        return ""
    out["acao_grupo"] = acao_mix.apply(_grp)

    if data_col and hora_col:
        out["data_acao"] = pd.to_datetime(
            normalize_str(df[data_col]).fillna("") + " " + normalize_str(df[hora_col]).fillna(""),
            errors="coerce", dayfirst=True
        )
    elif data_col:
        out["data_acao"] = pd.to_datetime(df[data_col], errors="coerce", dayfirst=True)
    else:
        out["data_acao"] = pd.NaT

    out["acao_codigo_raw"] = acao_code
    out["acao_desc_raw"] = acao_desc
    out["inclusao_cliente"] = normalize_str(df[inclusao_col]) if inclusao_col else ""
    out["data_abertura_cnpj"] = pd.to_datetime(df[abertura_col], errors="coerce", dayfirst=True) if abertura_col else pd.NaT
    out = out[out["cnpj"] != ""].copy()
    return out

def _extract_leads_base(df_leads: pd.DataFrame) -> pd.DataFrame:
    df = df_leads.copy()
    cnpj_col = _coalesce_col(df, ["CNPJ_CLIENTE", "CNPJ", "CD_CPF_CNPJ_CLIENTE", "CPF / CNPJ"])
    nome_col = _coalesce_col(df, ["NOME_CLIENTE", "CLIENTE", "NOME", "Nome cliente", "Nome"])
    nome_responsavel_col = _coalesce_col(df, ["NOME_RESPONSAVEL", "NOME RESPONSAVEL", "RESPONSAVEL"])
    telefone_col = _coalesce_col(df, ["CELULAR_RESPONSAVEL", "CELULAR RESPONSAVEL", "TELEFONE_RESPONSAVEL", "TELEFONE", "CELULAR"])
    data_base_col = _coalesce_col(df, ["DATA_BASE"])
    cadastro_col = _coalesce_col(df, ["DATA_HORA_CADASTRO", "DATA_CADASTRO"])
    aberta_col = _coalesce_col(df, ["DT_CONTA_ABERTA", "DT_CONTA_CRIADA"])
    status_abertura_col = _coalesce_col(df, ["STATUS_ABERTURA_CONTA"])
    status_final_col = _coalesce_col(df, ["STATUS_FINAL"])
    pendencias_col = _coalesce_col(df, ["PENDENCIAS"])

    out = pd.DataFrame()
    out["cnpj"] = _normalize_cnpj_series(df[cnpj_col]) if cnpj_col else ""
    out["nome_cliente"] = normalize_str(df[nome_col]) if nome_col else ""
    out["nome_responsavel"] = normalize_str(df[nome_responsavel_col]) if nome_responsavel_col else ""
    if telefone_col:
        out["telefone"] = normalize_str(df[telefone_col])
    elif len(df.columns) > 6:
        out["telefone"] = normalize_str(df.iloc[:, 6])
    else:
        out["telefone"] = ""
    out["data_base"] = pd.to_datetime(df[data_base_col], errors="coerce", dayfirst=True) if data_base_col else pd.NaT
    if cadastro_col:
        out["data_hora_cadastro"] = pd.to_datetime(df[cadastro_col], errors="coerce", dayfirst=True)
    elif len(df.columns) > 12:
        out["data_hora_cadastro"] = pd.to_datetime(df.iloc[:, 12], errors="coerce", dayfirst=True)
    else:
        out["data_hora_cadastro"] = pd.NaT
    out["dt_conta_aberta_leads"] = pd.to_datetime(df[aberta_col], errors="coerce", dayfirst=True) if aberta_col else pd.NaT
    out["status_abertura_conta"] = normalize_str(df[status_abertura_col]) if status_abertura_col else ""
    out["status_final"] = normalize_str(df[status_final_col]) if status_final_col else ""
    status_vazio = out["status_abertura_conta"].apply(_normalize_person_key).isin(["", "-", "'-", "'"])
    out.loc[status_vazio, "status_abertura_conta"] = out.loc[status_vazio, "status_final"]
    out["pendencias"] = normalize_str(df[pendencias_col]) if pendencias_col else ""
    if out["nome_cliente"].apply(_normalize_text_value).eq("").all() and len(df.columns) > 4:
        out["nome_cliente"] = normalize_str(df.iloc[:, 4])
    out["nome_cliente"] = out["nome_cliente"].replace("", pd.NA).fillna(out["nome_responsavel"]).fillna("")
    out["nome_envio"] = out["nome_responsavel"].replace("", pd.NA).fillna(out["nome_cliente"])
    out["mes_ref"] = out["data_hora_cadastro"].dt.strftime("%Y-%m").fillna("")
    out = out[out["cnpj"] != ""].copy()
    out = out.sort_values(["cnpj", "data_hora_cadastro"]).drop_duplicates(subset=["cnpj"], keep="last")
    return out

def _extract_visao_base(df_visao: pd.DataFrame) -> pd.DataFrame:
    df = df_visao.copy()
    cnpj_col = _coalesce_col(df, ["CD_CPF_CNPJ_CLIENTE", "CNPJ_CLIENTE", "CNPJ"])
    nome_col = _coalesce_col(df, ["NOME_CLIENTE"])
    data_base_col = _coalesce_col(df, ["DATA_BASE"])
    dt_conta_col = _coalesce_col(df, ["DT_CONTA_CRIADA"])
    fund_col = _coalesce_col(df, ["DT_FUNDACAO_EMPRESA"])
    tel1_col = _coalesce_col(df, ["TELEFONE", "TELEFONE_1", "TELEFONE1", "TEL1", "TEL 1", "TELEFONE CLIENTE"])
    tel2_col = _coalesce_col(df, ["TELEFONE_MASTER", "TELEFONE_2", "TELEFONE2", "TEL2", "TEL 2", "CELULAR"])
    pix_col = _coalesce_col(df, ["CHAVES_PIX_FORTE"])
    cashin_col = _coalesce_col(df, ["VL_CASH_IN_MTD"])
    wallet_col = _coalesce_col(df, ["FL_WALLET_CADASTRADA", "WALLET", "FL_WALLET", "CARTAO_WALLET"])
    pay_prop_col = _coalesce_col(df, ["FL_PROPENSAO_C6PAY"])
    pay_tpv_col = _coalesce_col(df, ["TPV_C6PAY_POTENCIAL"])
    pay_eleg_col = _coalesce_col(df, ["FL_ELEGIVEL_VENDA_C6PAY"])
    pay_status_col = _coalesce_col(df, ["STATUS_PROPOSTA_SF_PAY"])
    pay_aprov_col = _coalesce_col(df, ["DT_APROVACAO_PAY"])
    pay_inst_col = _coalesce_col(df, ["DT_INSTALL_MAQ"])
    pay_ativ_col = _coalesce_col(df, ["DT_ATIVACAO_PAY"])
    pay_ativa30_col = _coalesce_col(df, ["C6PAY_ATIVA_30"])
    pay_ult_col = _coalesce_col(df, ["DT_ULT_TRANS_PAY"])
    cart_entrega_col = _coalesce_col(df, ["DT_ENTREGA_CARTAO"])
    cart_ativ_col = _coalesce_col(df, ["DT_ATIV_CARTAO_CRED"])
    banco_col = _coalesce_col(df, ["BANCO_DOMICILIO"])
    saldo_col = _coalesce_col(df, ["VL_SALDO_MEDIO_MENSALIZADO"])
    mes_col = _coalesce_col(df, ["MES_REF_COMISS"])
    qual_col = _coalesce_col(df, ["FL_QUALIFICADO_COMISS"])
    crit_col = _coalesce_col(df, ["CRITERIOS_ATINGIDOS_COMISS"])

    out = pd.DataFrame()
    out["cnpj"] = _normalize_cnpj_series(df[cnpj_col]) if cnpj_col else ""
    out["nome_cliente"] = normalize_str(df[nome_col]) if nome_col else ""
    out["data_base"] = pd.to_datetime(df[data_base_col], errors="coerce", dayfirst=True) if data_base_col else pd.NaT
    out["dt_conta_criada"] = pd.to_datetime(df[dt_conta_col], errors="coerce", dayfirst=True) if dt_conta_col else pd.NaT
    out["dt_fundacao_empresa"] = pd.to_datetime(df[fund_col], errors="coerce", dayfirst=True) if fund_col else pd.NaT
    out["telefone"] = normalize_str(df[tel1_col]) if tel1_col else ""
    out["telefone_master"] = normalize_str(df[tel2_col]) if tel2_col else ""
    out["pix_raw"] = normalize_str(df[pix_col]) if pix_col else ""
    out["pix"] = df[pix_col].apply(lambda x: _pix_clean_value(x) if _pix_is_valid(x) else "") if pix_col else ""
    out["tem_pix"] = out["pix"].apply(_pix_is_valid) if pix_col else False
    out["pix_tipo"] = out["pix"]
    out["wallet"] = normalize_str(df[wallet_col]) if wallet_col else ""
    out["vl_cash_in_mtd"] = pd.to_numeric(df[cashin_col], errors="coerce").fillna(0.0) if cashin_col else 0.0
    out["fl_propensao_c6pay"] = normalize_str(df[pay_prop_col]) if pay_prop_col else ""
    out["tpv_c6pay_potencial"] = pd.to_numeric(df[pay_tpv_col], errors="coerce").fillna(0.0) if pay_tpv_col else 0.0
    out["fl_elegivel_venda_c6pay"] = normalize_str(df[pay_eleg_col]) if pay_eleg_col else ""
    out["status_proposta_sf_pay"] = normalize_str(df[pay_status_col]) if pay_status_col else ""
    out["dt_aprovacao_pay"] = pd.to_datetime(df[pay_aprov_col], errors="coerce", dayfirst=True) if pay_aprov_col else pd.NaT
    out["dt_install_maq"] = pd.to_datetime(df[pay_inst_col], errors="coerce", dayfirst=True) if pay_inst_col else pd.NaT
    out["dt_ativacao_pay"] = pd.to_datetime(df[pay_ativ_col], errors="coerce", dayfirst=True) if pay_ativ_col else pd.NaT
    out["c6pay_ativa_30"] = normalize_str(df[pay_ativa30_col]) if pay_ativa30_col else ""
    out["dt_ult_trans_pay"] = pd.to_datetime(df[pay_ult_col], errors="coerce", dayfirst=True) if pay_ult_col else pd.NaT
    out["dt_entrega_cartao"] = pd.to_datetime(df[cart_entrega_col], errors="coerce", dayfirst=True) if cart_entrega_col else pd.NaT
    out["dt_ativ_cartao_cred"] = pd.to_datetime(df[cart_ativ_col], errors="coerce", dayfirst=True) if cart_ativ_col else pd.NaT
    out["banco_domicilio"] = normalize_str(df[banco_col]) if banco_col else ""
    out["vl_saldo_medio_mensalizado"] = pd.to_numeric(df[saldo_col], errors="coerce").fillna(0.0) if saldo_col else 0.0
    out["mes_ref_comiss"] = normalize_str(df[mes_col]).str.upper() if mes_col else ""
    out["fl_qualificado_comiss"] = pd.to_numeric(df[qual_col], errors="coerce").fillna(0).astype(int) if qual_col else 0
    out["criterios_atingidos_comiss"] = normalize_str(df[crit_col]) if crit_col else ""
    out["nivel_maximo"] = out["criterios_atingidos_comiss"].apply(parse_level_from_criterios)
    out["qualificado"] = (out["nivel_maximo"] >= 1) | (out["fl_qualificado_comiss"] >= 1)
    out["estagio_m"] = out.apply(lambda r: r["mes_ref_comiss"] if str(r["mes_ref_comiss"]).strip() in {"M0","M1","M2"} else _stage_from_open_date(r["dt_conta_criada"], r["data_base"]), axis=1)
    out = out[out["cnpj"] != ""].copy()
    out = out.sort_values(["cnpj", "data_base"]).drop_duplicates(subset=["cnpj"], keep="last")
    return out


def _excel_col_name(df: pd.DataFrame, letters: str) -> Optional[str]:
    if df is None or df.empty:
        return None
    idx = 0
    for ch in str(letters or "").strip().upper():
        if not ("A" <= ch <= "Z"):
            continue
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    idx -= 1
    if 0 <= idx < len(df.columns):
        return df.columns[idx]
    return None


def _series_by_name_or_letter(df: pd.DataFrame, names: List[str], letters: Optional[str] = None, default="") -> pd.Series:
    col = _coalesce_col(df, names)
    if col is None and letters:
        col = _excel_col_name(df, letters)
    if col is None:
        return pd.Series([default] * len(df), index=df.index)
    return df[col]


def _msg_open_account_phone(v) -> str:
    if v is None or pd.isna(v):
        return ""
    if isinstance(v, (int, np.integer)):
        raw = str(int(v))
    elif isinstance(v, (float, np.floating)) and np.isfinite(v):
        raw = str(int(v)) if float(v).is_integer() else str(v)
    else:
        raw = str(v or "").strip()
        if raw.endswith(".0") and re.fullmatch(r"\d+\.0", raw):
            raw = raw[:-2]
    digits = re.sub(r"\D+", "", raw)
    if not digits:
        return ""
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("55"):
        return digits
    return "55" + digits


def _phone_without_55(v) -> str:
    digits = re.sub(r"\D+", "", str(v or ""))
    if digits.startswith("55") and len(digits) > 11:
        return digits[2:]
    return digits


def _read_blacklist_upload(uploaded_file) -> pd.DataFrame:
    if uploaded_file is None:
        return pd.DataFrame()
    raw = uploaded_file.getvalue()
    name = str(getattr(uploaded_file, "name", "") or "").lower()
    if name.endswith(".csv"):
        for enc in ["utf-8-sig", "utf-8", "latin1"]:
            try:
                sample = raw[:100_000].decode(enc, errors="replace")
                sep = _detect_delim_for_csv(sample) if "_detect_delim_for_csv" in globals() else ";"
                return pd.read_csv(io.BytesIO(raw), sep=sep, dtype="string", encoding=enc, engine="python", on_bad_lines="skip")
            except Exception:
                continue
        return pd.read_csv(io.BytesIO(raw), sep=None, dtype="string", engine="python", on_bad_lines="skip")
    return read_excel_any(raw).astype("string")


def _blacklist_sets_from_df(df_black: pd.DataFrame) -> Tuple[set, set]:
    if df_black is None or df_black.empty:
        return set(), set()
    cnpj_cols = [c for c in df_black.columns if "CNPJ" in _normalize_person_key(c) or "CPF" in _normalize_person_key(c)]
    phone_cols = [c for c in df_black.columns if any(k in _normalize_person_key(c) for k in ["TELEFONE", "FONE", "CELULAR", "WHATS"])]
    if not cnpj_cols and len(df_black.columns) >= 1:
        cnpj_cols = [df_black.columns[0]]
    if not phone_cols and len(df_black.columns) >= 2:
        phone_cols = [df_black.columns[1]]
    cnpjs = set()
    phones = set()
    for col in cnpj_cols:
        for val in df_black[col].dropna().tolist():
            cnpj = _normalize_cnpj_text(val)
            if cnpj:
                cnpjs.add(cnpj)
    for col in phone_cols:
        for val in df_black[col].dropna().tolist():
            phone = _phone_without_55(val)
            if phone:
                phones.add(phone)
    return cnpjs, phones


def _apply_ura_blacklist(df_ura: pd.DataFrame, blacklist_cnpjs: set, blacklist_phones: set) -> pd.DataFrame:
    if df_ura is None or df_ura.empty:
        return pd.DataFrame()
    out = df_ura.copy()
    cnpj_norm = out.get("CNPJ", pd.Series([""] * len(out), index=out.index)).apply(_normalize_cnpj_text)
    tel1_norm = out.get("TELEFONE1", pd.Series([""] * len(out), index=out.index)).apply(_phone_without_55)
    tel2_norm = out.get("TELEFONE2", pd.Series([""] * len(out), index=out.index)).apply(_phone_without_55)
    remove = cnpj_norm.isin(blacklist_cnpjs) | tel1_norm.isin(blacklist_phones) | tel2_norm.isin(blacklist_phones)
    return out.loc[~remove].copy()


def _build_open_accounts_actions_report(df_visao: pd.DataFrame) -> pd.DataFrame:
    if df_visao is None or df_visao.empty:
        return pd.DataFrame()
    df = df_visao.copy()
    data_abertura_all = pd.to_datetime(_series_by_name_or_letter(df, [COL_ABERTURA, "DATA CONTA CRIADA", "DT CONTA CRIADA"], "T"), errors="coerce", dayfirst=True)
    status_all = normalize_str(_series_by_name_or_letter(df, [COL_STATUS, "STATUS", "STATUS_CONTA", "STATUS CC"], "V")).str.upper()
    mask = ~status_all.str.contains("BLOQUEAD|DESATIVAD|ENCERRAD|CANCEL", na=False)
    base = df.loc[mask].copy()
    if base.empty:
        return pd.DataFrame()

    data_abertura = data_abertura_all.loc[base.index]
    cnpj_s = _series_by_name_or_letter(base, [COL_CNPJ, "CNPJ", "CPF_CNPJ"], "C").astype("string").fillna("")
    nome_s = _series_by_name_or_letter(base, ["NOME_CLIENTE", "NOME CLIENTE", "CLIENTE", "NOME"], "D").astype("string").fillna("")
    uf_s = _series_by_name_or_letter(base, ["UF"], "J").astype("string").fillna("")
    fund_s = pd.to_datetime(_series_by_name_or_letter(base, [COL_FUNDACAO, "DATA FUNDACAO EMPRESA", "DT FUNDACAO EMPRESA"], "P"), errors="coerce", dayfirst=True)
    ramo_s = _series_by_name_or_letter(base, ["RAMO_ATUACAO", "RAMO ATUACAO", "RAMO_ATIVIDADE", "RAMO DE ATIVIDADE"], "Q").astype("string").fillna("")
    conta_s = _series_by_name_or_letter(base, ["NUM_CONTA", "NUMERO_CONTA", "NUMERO DA CONTA"], "R").astype("string").fillna("")
    pix_s = _series_by_name_or_letter(base, [COL_PIX, "CHAVES PIX FORTE", "CHAVE_PIX"], "X").astype("string").fillna("")
    cash_s = pd.to_numeric(_series_by_name_or_letter(base, [COL_CASHIN_MTD, "VALOR CASHIN", "VALOR CASH IN", "VL CASH IN MTD"], "Y"), errors="coerce").fillna(0.0)
    entrega_s = pd.to_datetime(_series_by_name_or_letter(base, ["DT_ENTREGA_CARTAO", "DATA ENTREGA CARTAO", "DATA ENTREGA CARTÃO"], "AB"), errors="coerce", dayfirst=True)
    limite_cartao_s = pd.to_numeric(_series_by_name_or_letter(base, ["LIMITE_CARTAO", "LIMITE CARTAO", "LIMITE CARTÃO"], "Z"), errors="coerce").fillna(0.0)
    limite_cdb_s = pd.to_numeric(_series_by_name_or_letter(base, ["LIMITE_ALOCADO_CARTAO_CDB", "LIMITE ALOCADO CARTAO CDB", "LIMITE ALOCADO CARTÃO CDB"], "AA"), errors="coerce").fillna(0.0)
    mes_ref_s = _series_by_name_or_letter(base, [COL_BR, "MES_REF_COMISS", "MES REFERENCIA COMISSAO"], "BO").astype("string").fillna("")
    tpv_m0_s = pd.to_numeric(_series_by_name_or_letter(base, ["TPV_M0", "TPVM0", "TPV M0"], "AS"), errors="coerce").fillna(0.0)
    criterios_s = _series_by_name_or_letter(base, [COL_CRIT, "CRITERIOS_ATINGIDOS_COMISS", "CRITÉRIOS ATINGIDOS"], "BV").astype("string").fillna("")
    tel_1_s = _series_by_name_or_letter(base, ["TELEFONE", "TELEFONE_1", "TEL", "FONE"], "M")
    tel_2_s = _series_by_name_or_letter(base, ["TELEFONE_MASTER", "TELEFONE_2", "TEL2", "FONE2", "CELULAR"], "N")

    rows = []
    for idx in base.index:
        telefones = []
        for raw_phone in [tel_1_s.loc[idx], tel_2_s.loc[idx]]:
            phone = _msg_open_account_phone(raw_phone)
            if phone and phone not in telefones:
                telefones.append(phone)
        for phone in telefones:
            rows.append({
                "telefone": phone,
                "cnpj": cnpj_s.loc[idx],
                "nome_cliente": nome_s.loc[idx],
                "uf": uf_s.loc[idx],
                "dt_fundacao_empresa": fmt_date(fund_s.loc[idx]) if pd.notna(fund_s.loc[idx]) else "",
                "ramo_atividade": ramo_s.loc[idx],
                "num_conta": conta_s.loc[idx],
                "dt_conta_criada": fmt_date(data_abertura.loc[idx]) if pd.notna(data_abertura.loc[idx]) else "",
                "chaves_pix_forte": pix_s.loc[idx],
                "valor_cashin": float(cash_s.loc[idx] or 0.0),
                "dt_entrega_cartao": fmt_date(entrega_s.loc[idx]) if pd.notna(entrega_s.loc[idx]) else "",
                "limite_cartao": float(limite_cartao_s.loc[idx] or 0.0),
                "limite_alocado_cartao_cdb": float(limite_cdb_s.loc[idx] or 0.0),
                "mes_ref_comissao": mes_ref_s.loc[idx],
                "tpv_m0": float(tpv_m0_s.loc[idx] or 0.0),
                "criterios_atingidos": criterios_s.loc[idx],
            })
    return pd.DataFrame(rows)


def _open_accounts_actions_summary(df_visao: pd.DataFrame) -> dict:
    if df_visao is None or df_visao.empty:
        return {"linhas_base": 0, "linhas_telefone_brutas": 0, "telefones_duplicados_cliente": 0, "sem_telefone": 0, "contas_criadas": 0, "excluidas_status": 0, "clientes_validos": 0}
    data_abertura = pd.to_datetime(_series_by_name_or_letter(df_visao, [COL_ABERTURA, "DATA CONTA CRIADA", "DT CONTA CRIADA"], "T"), errors="coerce", dayfirst=True)
    status_s = normalize_str(_series_by_name_or_letter(df_visao, [COL_STATUS, "STATUS", "STATUS_CONTA", "STATUS CC"], "V")).str.upper()
    has_open = data_abertura.notna()
    blocked = status_s.str.contains("BLOQUEAD|DESATIVAD|ENCERRAD|CANCEL", na=False)
    tel_1_s = _series_by_name_or_letter(df_visao, ["TELEFONE", "TELEFONE_1", "TEL", "FONE"], "M")
    tel_2_s = _series_by_name_or_letter(df_visao, ["TELEFONE_MASTER", "TELEFONE_2", "TEL2", "FONE2", "CELULAR"], "N")
    valid_mask = ~blocked
    raw_phone_lines = int(valid_mask.sum()) * 2
    duplicates = 0
    no_phone = 0
    for idx in df_visao.loc[valid_mask].index:
        p1 = _msg_open_account_phone(tel_1_s.loc[idx])
        p2 = _msg_open_account_phone(tel_2_s.loc[idx])
        if not p1 and not p2:
            no_phone += 1
        if p1 and p2 and p1 == p2:
            duplicates += 1
    return {
        "linhas_base": int(len(df_visao)),
        "linhas_telefone_brutas": raw_phone_lines,
        "telefones_duplicados_cliente": int(duplicates),
        "sem_telefone": int(no_phone),
        "contas_criadas": int(has_open.sum()),
        "excluidas_status": int(blocked.sum()),
        "clientes_validos": int(valid_mask.sum()),
    }


def _read_lct_file_any(name: str, raw_bytes: bytes) -> Optional[pd.DataFrame]:
    try:
        if str(name or "").lower().endswith(".csv"):
            sample = raw_bytes[:200_000].decode("utf-8-sig", errors="replace")
            sep = _detect_delim_for_csv(sample) if "_detect_delim_for_csv" in globals() else ","
            if sep == ";":
                lines = sample.splitlines()
                first_data = lines[1].split(";") if len(lines) > 1 else []
                header = lines[0].split(";") if lines else []
                if len(header) == 16 and len(first_data) == 17:
                    text = raw_bytes.decode("utf-8-sig", errors="replace")
                    rows = []
                    for ln in text.splitlines()[1:]:
                        parts = ln.split(";")
                        if len(parts) < 17:
                            parts = parts + ([""] * (17 - len(parts)))
                        elif len(parts) > 17:
                            parts = parts[:16] + [";".join(parts[16:])]
                        rows.append(parts)
                    fixed_cols = [
                        "Nome", "Cód", "CPF / CNPJ", "Agente Flag", "Agente", "Ação", "Data", "Hora",
                        "Histórico", "Fila", "Fone Discado", "Credor", "Atraso", "Valor", "Inclusão", "CDEC", "Fase"
                    ]
                    return pd.DataFrame(rows, columns=fixed_cols, dtype=str)
            return pd.read_csv(io.BytesIO(raw_bytes), sep=sep, engine="python", on_bad_lines="skip", encoding="utf-8-sig")
        return pd.read_excel(io.BytesIO(raw_bytes))
    except Exception as e:
        st.error(f"Erro ao ler Resumo LCT: {e}")
        return None


def _read_temp_import_file_any(path: str) -> Optional[pd.DataFrame]:
    try:
        name = os.path.basename(str(path or ""))
        _, mtime_ns, size = _safe_file_signature(path)
        if name.lower().endswith(".csv"):
            with open(path, "rb") as f:
                raw = f.read()
            return _read_lct_file_any(name, raw)
        return _read_excel_path_cached(path, mtime_ns, size).copy()
    except Exception:
        return None


def _load_lct_history_from_temp_imports(cached_lct: Optional[pd.DataFrame] = None) -> Tuple[pd.DataFrame, str]:
    frames, names = _load_lct_temp_history_cached()
    frames = [df.copy() for df in frames]
    names = list(names)
    if cached_lct is not None and not cached_lct.empty:
        frames.append(cached_lct.copy())
    if not frames:
        return pd.DataFrame(), ""
    return pd.concat(frames, ignore_index=True), ", ".join(sorted(set(names)))


@st.cache_data(show_spinner=False)
def _load_lct_temp_history_cached(_version: str = "lct-v3") -> Tuple[List[pd.DataFrame], List[str]]:
    frames = []
    names = []
    for path in _temp_import_files_by_keyword("resumo lct"):
        df = _read_temp_import_file_any(path)
        if df is None or df.empty:
            continue
        frames.append(df)
        names.append(os.path.basename(path))
    return frames, names


def _load_funil_history_from_temp_imports(keyword: str, _extractor) -> pd.DataFrame:
    frames = [df.copy() for df in _load_funil_temp_history_cached(keyword, _extractor)]
    cache_kind = ""
    key_norm = unicodedata.normalize("NFKD", str(keyword or "")).encode("ascii", "ignore").decode("ascii").lower()
    if "lead" in key_norm:
        cache_kind = "leads"
    elif "visao" in key_norm:
        cache_kind = "visao"
    if cache_kind:
        cached_df, _, _ = _load_daily_import_cache(cache_kind)
        if cached_df is not None and not cached_df.empty:
            try:
                extracted = _extractor(cached_df)
            except Exception:
                extracted = pd.DataFrame()
            if extracted is not None and not extracted.empty:
                frames.append(extracted)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    if "cnpj" in out.columns:
        sort_cols = [c for c in ["cnpj", "data_base", "data_hora_cadastro", "dt_conta_criada"] if c in out.columns]
        if sort_cols:
            out = out.sort_values(sort_cols)
        out = out.drop_duplicates(subset=["cnpj"], keep="last")
    return out.reset_index(drop=True)


@st.cache_data(show_spinner=False)
def _load_funil_temp_history_cached(keyword: str, _extractor) -> List[pd.DataFrame]:
    frames = []
    for path in _temp_import_files_by_keyword(keyword):
        df = _read_temp_import_file_any(path)
        if df is None or df.empty:
            continue
        try:
            extracted = _extractor(df)
        except Exception:
            continue
        if extracted is not None and not extracted.empty:
            frames.append(extracted)
    return frames


def _extract_lct_base(df_lct: pd.DataFrame, source_keywords: Optional[List[str]] = None) -> pd.DataFrame:
    df = df_lct.copy()
    nome_col = _coalesce_col(df, ["nome_cliente_lct", "nome_cliente_index", "Nome", "NOME", "NOME_CLIENTE"])
    cnpj_col = _coalesce_col(df, ["cnpj", "CPF / CNPJ", "CNPJ", "CNPJ_CLIENTE"])
    data_col = _coalesce_col(df, ["data_lct", "Data", "DATA"])
    fase_col = _coalesce_col(df, ["dt_fundacao_lct", "Fase", "FASE"])
    acao_col = _coalesce_col(df, ["acao_lct", "Ação", "ACAO", "Acao"])
    hist_col = _coalesce_col(df, ["origem_lct_texto", "Histórico", "HISTORICO", "HISTÓRICO", "Historico"])

    shifted_lct = False
    if cnpj_col and "Cód" in df.columns:
        cnpj_valid = _normalize_cnpj_series(df[cnpj_col]).astype(str).str.len().ge(14).mean()
        cod_valid = _normalize_cnpj_series(df["Cód"]).astype(str).str.len().ge(14).mean()
        shifted_lct = bool(cod_valid > cnpj_valid and cod_valid > 0.5)
    if shifted_lct:
        cnpj_col = "Cód"
        acao_col = "Unnamed: 4" if "Unnamed: 4" in df.columns else acao_col
        data_col = "Ação" if "Ação" in df.columns else data_col
        hist_col = "Hora" if "Hora" in df.columns else hist_col

    out = pd.DataFrame()
    out["nome_cliente_lct"] = normalize_str(df[nome_col]) if nome_col else ""
    out["cnpj"] = _normalize_cnpj_series(df[cnpj_col]) if cnpj_col else ""
    if data_col:
        data_raw = df[data_col].astype("string").fillna("").str.strip()
        data_dt = pd.to_datetime(data_raw, errors="coerce", dayfirst=True)
        if data_dt.isna().all():
            data_dt = pd.to_datetime(data_raw.str.extract(r"(\d{2}/\d{2}/\d{4})", expand=False), errors="coerce", dayfirst=True)
        out["data_lct"] = data_dt
    else:
        out["data_lct"] = pd.NaT
    if fase_col:
        fase_raw = df[fase_col].astype("string").fillna("").str.strip()
        fase_dt = pd.to_datetime(fase_raw, errors="coerce", dayfirst=True)
        if fase_dt.isna().all():
            fase_dt = pd.to_datetime(fase_raw.str.extract(r"(\d{2}/\d{2}/\d{4})", expand=False), errors="coerce", dayfirst=True)
        out["dt_fundacao_lct"] = fase_dt
    else:
        out["dt_fundacao_lct"] = pd.NaT
    out["acao_lct"] = normalize_str(df[acao_col]).str.upper() if acao_col else ""
    hist_txt = normalize_str(df[hist_col]).str.upper() if hist_col else pd.Series([""] * len(df), index=df.index)
    out["origem_lct_texto"] = (out["acao_lct"].astype(str) + " " + hist_txt.astype(str)).str.strip()
    out = out[out["cnpj"] != ""].copy()
    keywords = [str(k or "").strip().upper() for k in (source_keywords or ["LCT"]) if str(k or "").strip()]
    if keywords and "origem_lct_texto" in out.columns and out["origem_lct_texto"].astype(str).str.strip().ne("").any():
        pattern = "|".join(re.escape(k) for k in keywords)
        out = out[out["origem_lct_texto"].astype(str).str.contains(pattern, na=False, regex=True)].copy()
    out["data_lct_dia"] = pd.to_datetime(out["data_lct"], errors="coerce").dt.date
    if out["data_lct_dia"].isna().all() and data_col:
        out["data_lct_dia"] = pd.to_datetime(df.loc[out.index, data_col], errors="coerce", dayfirst=True).dt.date
    out = out.sort_values(["data_lct", "cnpj"]).drop_duplicates(subset=["cnpj", "data_lct_dia"], keep="last")
    return out


def _compact_lct_cache_df(df_lct: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    if df_lct is None or df_lct.empty:
        return df_lct
    try:
        extracted = _extract_lct_base(df_lct, ["LCT", "URA", "AURA", "SBM", "SABER MAIS", "SABERMAIS"])
    except Exception:
        extracted = pd.DataFrame()
    if extracted is None or extracted.empty:
        df = df_lct.copy()
        nome_col = _coalesce_col(df, ["nome_cliente_lct", "nome_cliente_index", "Nome", "NOME", "NOME_CLIENTE"])
        cnpj_col = _coalesce_col(df, ["cnpj", "CPF / CNPJ", "CNPJ", "CNPJ_CLIENTE", "Cód"])
        data_col = _coalesce_col(df, ["data_lct", "Data", "DATA", "Ação"])
        fase_col = _coalesce_col(df, ["dt_fundacao_lct", "Fase", "FASE"])
        acao_col = _coalesce_col(df, ["acao_lct", "Unnamed: 4", "Ação", "ACAO", "Acao"])
        extracted = pd.DataFrame(index=df.index)
        extracted["nome_cliente_lct"] = normalize_str(df[nome_col]) if nome_col else ""
        extracted["cnpj"] = _normalize_cnpj_series(df[cnpj_col]) if cnpj_col else ""
        extracted["data_lct"] = pd.to_datetime(df[data_col], errors="coerce", dayfirst=True) if data_col else pd.NaT
        extracted["dt_fundacao_lct"] = pd.to_datetime(df[fase_col], errors="coerce", dayfirst=True) if fase_col else pd.NaT
        extracted["acao_lct"] = normalize_str(df[acao_col]).str.upper() if acao_col else ""
        extracted = extracted[extracted["cnpj"].astype(str).str.len().ge(14)].copy()
    if extracted is None or extracted.empty:
        return pd.DataFrame(columns=["nome_cliente_lct", "cnpj", "data_lct", "dt_fundacao_lct", "acao_lct", "origem_lct_texto"])
    if "acao_lct" in extracted.columns:
        extracted["acao_lct"] = normalize_str(extracted["acao_lct"]).str.upper()
    else:
        extracted["acao_lct"] = ""
    extracted["origem_lct_texto"] = extracted["acao_lct"]
    keep_cols = [
        "nome_cliente_lct",
        "cnpj",
        "data_lct",
        "dt_fundacao_lct",
        "acao_lct",
        "origem_lct_texto",
    ]
    return extracted[[c for c in keep_cols if c in extracted.columns]].reset_index(drop=True)


def _build_lct_source_reports(
    df_panel_lct: Optional[pd.DataFrame],
    leads_funil: pd.DataFrame,
    visao_funil: pd.DataFrame,
    source_label: str,
    source_keywords: List[str],
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    lct_work = _extract_lct_base(df_panel_lct, source_keywords) if df_panel_lct is not None else pd.DataFrame()
    if lct_work.empty:
        return pd.DataFrame(), pd.DataFrame()

    leads_idx = leads_funil[["cnpj", "data_hora_cadastro", "status_abertura_conta", "pendencias"]].copy() if not leads_funil.empty else pd.DataFrame(columns=["cnpj", "data_hora_cadastro", "status_abertura_conta", "pendencias"])
    visao_idx = visao_funil[["cnpj", "dt_conta_criada", "nome_cliente", "dt_fundacao_empresa"]].copy() if not visao_funil.empty else pd.DataFrame(columns=["cnpj", "dt_conta_criada", "nome_cliente", "dt_fundacao_empresa"])
    lct_work["cnpj"] = lct_work["cnpj"].apply(_normalize_cnpj_text)
    if not leads_idx.empty:
        leads_idx["cnpj"] = leads_idx["cnpj"].apply(_normalize_cnpj_text)
    if not visao_idx.empty:
        visao_idx["cnpj"] = visao_idx["cnpj"].apply(_normalize_cnpj_text)

    lct_merged = lct_work.merge(leads_idx, on="cnpj", how="left").merge(visao_idx, on="cnpj", how="left")
    lct_merged["nome_cliente_final"] = lct_merged["nome_cliente_lct"].replace("", pd.NA).fillna(lct_merged["nome_cliente"])
    lct_merged["fundacao_final"] = lct_merged["dt_fundacao_empresa"]
    fund_mask = pd.to_datetime(lct_merged["fundacao_final"], errors="coerce").isna()
    lct_merged.loc[fund_mask, "fundacao_final"] = lct_merged.loc[fund_mask, "dt_fundacao_lct"]
    lct_merged["indicado_banco"] = lct_merged["data_hora_cadastro"].notna()
    lct_merged["abriu_conta"] = lct_merged["dt_conta_criada"].notna()

    source_daily = (
        lct_work.groupby("data_lct_dia", dropna=True)
        .agg(clientes_origem=("cnpj", "nunique"))
        .reset_index()
        .sort_values("data_lct_dia", ascending=False)
    )
    indicados_daily = (
        lct_merged[lct_merged["indicado_banco"]]
        .groupby("data_lct_dia", dropna=True)["cnpj"]
        .nunique()
        .rename("clientes_indicados")
        .reset_index()
    )
    aberturas_daily = (
        lct_merged[lct_merged["abriu_conta"]]
        .groupby("data_lct_dia", dropna=True)["cnpj"]
        .nunique()
        .rename("contas_abertas")
        .reset_index()
    )
    source_daily = source_daily.merge(indicados_daily, on="data_lct_dia", how="left").merge(aberturas_daily, on="data_lct_dia", how="left")
    source_daily["clientes_indicados"] = pd.to_numeric(source_daily["clientes_indicados"], errors="coerce").fillna(0).astype(int)
    source_daily["contas_abertas"] = pd.to_numeric(source_daily["contas_abertas"], errors="coerce").fillna(0).astype(int)
    source_daily["% viraram indicação"] = (source_daily["clientes_indicados"] / source_daily["clientes_origem"].replace(0, pd.NA) * 100).fillna(0)
    source_daily["% abriram conta"] = (source_daily["contas_abertas"] / source_daily["clientes_origem"].replace(0, pd.NA) * 100).fillna(0)

    view_source = source_daily.copy()
    view_source["Data"] = pd.to_datetime(view_source["data_lct_dia"], errors="coerce").dt.strftime("%d/%m/%Y")
    view_source = view_source.rename(columns={
        "clientes_origem": f"Clientes {source_label}",
        "clientes_indicados": "Indicados no banco",
        "contas_abertas": "Abriram conta",
    })[["Data", f"Clientes {source_label}", "Indicados no banco", "Abriram conta", "% viraram indicação", "% abriram conta"]]

    analitico_source = lct_merged.copy()
    analitico_source[f"Data {source_label}"] = pd.to_datetime(analitico_source["data_lct"], errors="coerce").dt.strftime("%d/%m/%Y")
    analitico_source["Origem"] = source_label
    analitico_source["Fundação empresa"] = pd.to_datetime(analitico_source["fundacao_final"], errors="coerce").dt.strftime("%d/%m/%Y")
    analitico_source["Data cadastro banco"] = pd.to_datetime(analitico_source["data_hora_cadastro"], errors="coerce").dt.strftime("%d/%m/%Y")
    analitico_source["Data conta criada"] = pd.to_datetime(analitico_source["dt_conta_criada"], errors="coerce").dt.strftime("%d/%m/%Y")
    analitico_source["Indicado no banco"] = analitico_source["indicado_banco"].map({True: "SIM", False: "NÃO"})
    analitico_source["Abriu conta"] = analitico_source["abriu_conta"].map({True: "SIM", False: "NÃO"})
    analitico_source["CNPJ"] = analitico_source["cnpj"]
    analitico_source["Nome cliente"] = analitico_source["nome_cliente_final"]
    analitico_source = analitico_source[[f"Data {source_label}", "Origem", "Nome cliente", "CNPJ", "Fundação empresa", "Indicado no banco", "Data cadastro banco", "Abriu conta", "Data conta criada", "status_abertura_conta", "pendencias"]].rename(columns={"status_abertura_conta": "Status abertura", "pendencias": "Pendências"})
    return view_source, analitico_source


def _build_ura_reports(df_panel_lct: Optional[pd.DataFrame], leads_funil: pd.DataFrame, visao_funil: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    return _build_lct_source_reports(df_panel_lct, leads_funil, visao_funil, "URA", ["LCT", "URA", "AURA"])


def _build_sbm_reports(df_panel_lct: Optional[pd.DataFrame], leads_funil: pd.DataFrame, visao_funil: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    return _build_lct_source_reports(df_panel_lct, leads_funil, visao_funil, "SBM", ["SBM", "SABER MAIS", "SABERMAIS"])


def _build_followup_daily_outputs(leads_base: pd.DataFrame, df_visao_raw: Optional[pd.DataFrame] = None, previous_files=None) -> dict:
    followup_base = leads_base.copy() if isinstance(leads_base, pd.DataFrame) else pd.DataFrame()
    if followup_base.empty:
        return {"base": pd.DataFrame(), "analitico": pd.DataFrame(), "mensagens": pd.DataFrame(), "clientes_ura": pd.DataFrame(), "data_base_txt": dt.date.today().strftime("%d%m%Y")}

    if df_visao_raw is not None and not getattr(df_visao_raw, "empty", True):
        visao_follow = _extract_visao_base(df_visao_raw)
        visao_follow = visao_follow[[c for c in [
            "cnpj", "dt_conta_criada", "dt_fundacao_empresa", "telefone", "telefone_master",
            "pix", "wallet", "fl_propensao_c6pay", "fl_elegivel_venda_c6pay",
            "status_proposta_sf_pay", "dt_aprovacao_pay", "dt_install_maq", "dt_ativacao_pay", "banco_domicilio"
        ] if c in visao_follow.columns]].copy()
        followup_base = followup_base.merge(visao_follow, on="cnpj", how="left", suffixes=("", "_visao_follow"))
        for col in [
            "nome_cliente", "dt_conta_criada", "dt_fundacao_empresa", "telefone", "telefone_master",
            "pix", "wallet", "fl_propensao_c6pay", "fl_elegivel_venda_c6pay",
            "status_proposta_sf_pay", "dt_aprovacao_pay", "dt_install_maq", "dt_ativacao_pay", "banco_domicilio"
        ]:
            visao_col = f"{col}_visao_follow"
            if visao_col not in followup_base.columns:
                continue
            if col not in followup_base.columns:
                followup_base[col] = followup_base[visao_col]
                continue
            fill_mask = followup_base[col].apply(_normalize_text_value).eq("")
            followup_base.loc[fill_mask, col] = followup_base.loc[fill_mask, visao_col]

    if "nome_cliente" not in followup_base.columns:
        followup_base["nome_cliente"] = ""
    if "nome_envio" not in followup_base.columns:
        followup_base["nome_envio"] = followup_base["nome_cliente"]
    else:
        followup_base["nome_envio"] = followup_base["nome_envio"].fillna("").replace("", pd.NA).fillna(followup_base["nome_cliente"])
    for col, default_val in {"status_abertura_conta": "", "pendencias": "", "telefone": "", "data_hora_cadastro": pd.NaT}.items():
        if col not in followup_base.columns:
            followup_base[col] = default_val

    followup_base["dias_desde_cadastro"] = followup_base["data_hora_cadastro"].apply(lambda x: _days_since_today_exclusive(x, dt.date.today()))
    dt_abertura_ref = pd.to_datetime(followup_base.get("dt_abertura_ref", pd.Series(pd.NaT, index=followup_base.index)), errors="coerce")
    dt_conta_criada = pd.to_datetime(followup_base.get("dt_conta_criada", pd.Series(pd.NaT, index=followup_base.index)), errors="coerce")
    followup_base["abriu_conta_flag"] = np.where(dt_abertura_ref.notna() | dt_conta_criada.notna(), "SIM", "NÃO")
    followup_base = followup_base[followup_base["dias_desde_cadastro"].apply(lambda x: isinstance(x, (int, np.integer)) and 1 <= int(x) <= 15)].copy()
    followup_base = followup_base[followup_base["status_abertura_conta"].fillna("").apply(_is_actionable_followup_status)].copy()
    if "abriu_conta_flag" not in followup_base.columns:
        followup_base["abriu_conta_flag"] = "NÃO"
    followup_base = followup_base[followup_base["abriu_conta_flag"].ne("SIM")].copy()
    if "nome_cliente" not in followup_base.columns:
        followup_base["nome_cliente"] = ""
    if "nome_envio" not in followup_base.columns:
        followup_base["nome_envio"] = followup_base["nome_cliente"]
    else:
        followup_base["nome_envio"] = followup_base["nome_envio"].fillna("").replace("", pd.NA).fillna(followup_base["nome_cliente"])

    hist_prev = _build_previous_message_history(previous_files)
    nome_envio_series = followup_base["nome_envio"] if "nome_envio" in followup_base.columns else pd.Series([""] * len(followup_base), index=followup_base.index)
    followup_base["nome_key"] = nome_envio_series.fillna("").apply(_normalize_person_key)
    if not hist_prev.empty:
        followup_base = followup_base.merge(hist_prev, on="nome_key", how="left")
    else:
        followup_base["qtde_envios_anteriores"] = 0
        followup_base["ultima_data_envio"] = pd.NaT
        followup_base["ultima_msg_2"] = ""
        followup_base["ultima_msg_3"] = ""
        followup_base["ultima_msg_4"] = ""
    for col, default_val in {"qtde_envios_anteriores": 0, "ultima_msg_2": "", "ultima_msg_3": "", "ultima_msg_4": ""}.items():
        followup_base[col] = followup_base[col].fillna(default_val) if col in followup_base.columns else default_val
    if "ultima_data_envio" not in followup_base.columns:
        followup_base["ultima_data_envio"] = pd.NaT

    strategy_rows = pd.DataFrame([_lead_followup_strategy(r) for r in followup_base.to_dict("records")])
    if not strategy_rows.empty:
        followup_base = pd.concat([followup_base.reset_index(drop=True), strategy_rows.reset_index(drop=True)], axis=1)
    else:
        for col in ["foco_dia", "objetivo", "justificativa", "var_2", "var_3", "var_4"]:
            followup_base[col] = ""

    phone_pairs = followup_base.apply(lambda r: _focus_phone_pair(r), axis=1) if not followup_base.empty else []
    followup_base["telefone_1"] = [p[0] for p in phone_pairs]
    followup_base["telefone_2"] = [p[1] for p in phone_pairs]
    followup_base["linhas_envio_validas"] = followup_base.apply(lambda r: int(bool(r.get("telefone_1"))) + int(bool(r.get("telefone_2"))), axis=1) if not followup_base.empty else 0

    analitico_follow = followup_base[[c for c in [
        "nome_envio", "nome_cliente", "cnpj", "data_hora_cadastro", "dias_desde_cadastro",
        "status_abertura_conta", "pendencias", "telefone_1", "telefone_2",
        "qtde_envios_anteriores", "ultima_data_envio", "foco_dia", "objetivo",
        "justificativa", "dt_fundacao_empresa", "dt_conta_criada"
    ] if c in followup_base.columns]].copy()
    if not analitico_follow.empty:
        for col, fmt in {"data_hora_cadastro": "%d/%m/%Y %H:%M", "ultima_data_envio": "%d/%m/%Y", "dt_fundacao_empresa": "%d/%m/%Y", "dt_conta_criada": "%d/%m/%Y"}.items():
            if col in analitico_follow.columns:
                analitico_follow[col] = pd.to_datetime(analitico_follow[col], errors="coerce").dt.strftime(fmt)
    analitico_follow = analitico_follow.rename(columns={
        "nome_envio": "nome do cliente",
        "nome_cliente": "nome empresa",
        "cnpj": "CNPJ",
        "data_hora_cadastro": "data do cadastro",
        "dias_desde_cadastro": "dias desde o cadastro",
        "status_abertura_conta": "status atual",
        "pendencias": "pendências",
        "dt_fundacao_empresa": "data de fundação da empresa",
        "dt_conta_criada": "data de conta criada",
        "qtde_envios_anteriores": "envios anteriores",
        "ultima_data_envio": "última data de envio",
        "foco_dia": "foco do dia",
    })

    envio_msg_rows = []
    clientes_ura_rows = []
    for row in followup_base.to_dict("records"):
        telefones_com_55 = []
        telefones_sem_55 = []
        for raw_phone in [row.get("telefone_1", ""), row.get("telefone_2", "")]:
            phone_55 = re.sub(r"\D+", "", str(raw_phone or ""))
            if phone_55 and not phone_55.startswith("55"):
                phone_55 = f"55{phone_55}"
            phone_sem_55 = phone_55[2:] if phone_55.startswith("55") and len(phone_55) > 11 else phone_55
            if phone_55 and phone_55 not in telefones_com_55:
                telefones_com_55.append(phone_55)
            if phone_sem_55 and phone_sem_55 not in telefones_sem_55:
                telefones_sem_55.append(phone_sem_55)
        for phone_55 in telefones_com_55:
            envio_msg_rows.append({
                "telefone com 55": phone_55,
                "nome do cliente": row.get("nome_envio", row.get("nome_cliente", "")),
                "variável 2": row.get("var_2", ""),
                "variável 3": row.get("var_3", ""),
                "variável 4": row.get("var_4", ""),
            })
        phone_sem_55 = telefones_sem_55[0] if telefones_sem_55 else ""
        if phone_sem_55:
            clientes_ura_rows.append({
                "Nome": row.get("nome_cliente", row.get("nome_envio", "")),
                "CNPJ": re.sub(r"\D+", "", str(row.get("cnpj", "") or "")),
                "TELEFONE1": phone_sem_55,
                "TELEFONE2": "",
            })

    data_base_leads = pd.to_datetime(followup_base.get("data_base"), errors="coerce").max() if "data_base" in followup_base.columns else pd.NaT
    data_base_txt = data_base_leads.strftime("%d%m%Y") if pd.notna(data_base_leads) else dt.date.today().strftime("%d%m%Y")
    return {
        "base": followup_base,
        "analitico": analitico_follow,
        "mensagens": pd.DataFrame(envio_msg_rows),
        "clientes_ura": pd.DataFrame(clientes_ura_rows),
        "data_base_txt": data_base_txt,
    }


def _merge_action_latest(df_actions: pd.DataFrame, group_code: str, ref_df: pd.DataFrame, ref_date_col: str = "") -> pd.DataFrame:
    dfa = df_actions[df_actions["acao_grupo"] == group_code].copy()
    if dfa.empty:
        return pd.DataFrame(columns=["cnpj", "operador", "data_acao", "acao_grupo"])
    dfa = dfa.sort_values(["cnpj", "data_acao"])
    if ref_date_col and ref_date_col in ref_df.columns:
        out_rows = []
        ref_map = ref_df.set_index("cnpj")[ref_date_col].to_dict()
        for cnpj, grp in dfa.groupby("cnpj"):
            ref_dt = ref_map.get(cnpj)
            if pd.notna(ref_dt):
                ref_norm = pd.Timestamp(ref_dt).normalize()
                valid = grp[grp["data_acao"].notna() & (pd.to_datetime(grp["data_acao"], errors="coerce").dt.normalize() <= ref_norm)]
                chosen = valid.iloc[-1] if not valid.empty else grp.iloc[-1]
            else:
                chosen = grp.iloc[-1]
            out_rows.append(chosen)
        return pd.DataFrame(out_rows)
    return dfa.drop_duplicates(subset=["cnpj"], keep="last")

@st.cache_data(show_spinner=False)
def _to_excel_bytes(sheets: Dict[str, pd.DataFrame]) -> bytes:
    bio = io.BytesIO()
    with pd.ExcelWriter(bio, engine="openpyxl") as writer:
        for name, df in sheets.items():
            sheet = re.sub(r"[:\\/*?\[\]]", "_", name)[:31] or "Planilha"
            out = df.copy()
            for col in out.columns:
                if str(out[col].dtype).startswith("datetime64"):
                    out[col] = out[col].dt.strftime("%d/%m/%Y %H:%M:%S")
            out.to_excel(writer, index=False, sheet_name=sheet)
    bio.seek(0)
    return bio.getvalue()

def _load_c6_ops_history():
    return {
        "imports": safe_json_load(C6_OP_IMPORT_LOG, default=[]),
        "pix_track": safe_json_load(C6_OP_PIX_TRACK, default={}),
        "omc_maxpay": safe_json_load(C6_OP_OMC_MAXPAY, default={}),
    }

def _save_c6_ops_history(imports_log, pix_track, omc_maxpay):
    safe_json_save(C6_OP_IMPORT_LOG, imports_log)
    safe_json_save(C6_OP_PIX_TRACK, pix_track)
    safe_json_save(C6_OP_OMC_MAXPAY, omc_maxpay)


def _c6_default_remun_config() -> dict:
    base_rules = {
        "act_indicacao": 0.0,
        "semana": [
            {"min": 1, "max": 3, "valor": 2.50},
            {"min": 4, "max": 5, "valor": 5.00},
            {"min": 6, "max": 999, "valor": 7.00},
        ],
        "sabado": [
            {"min": 1, "max": 2, "valor": 5.00},
            {"min": 3, "max": 4, "valor": 10.00},
            {"min": 5, "max": 999, "valor": 15.00},
        ],
    }
    return {
        "categorias": {
            "clt": {"label": "Funcionários CLT", **json.loads(json.dumps(base_rules))},
            "estagiario": {"label": "Estagiários", **json.loads(json.dumps(base_rules))},
            "outros": {
                "label": "Outros",
                "act_indicacao": 0.0,
                "semana": [{"min": 1, "max": 999, "valor": 0.0}],
                "sabado": [{"min": 1, "max": 999, "valor": 0.0}],
            },
        },
        "operadores": {},
    }


def _default_operator_settings(categoria: str = "clt") -> dict:
    cat = str(categoria or "clt").strip().lower()
    if cat not in {"clt", "estagiario", "outros"}:
        cat = "clt"
    return {"categoria": cat, "ativo": True, "relatorio": True}


def _normalize_c6_remun_config(raw: Optional[dict] = None) -> dict:
    cfg = _c6_default_remun_config()
    raw = raw if isinstance(raw, dict) else {}
    raw_cats = raw.get("categorias") if isinstance(raw.get("categorias"), dict) else {}
    for key in ["clt", "estagiario", "outros"]:
        src = raw_cats.get(key, {}) if isinstance(raw_cats.get(key), dict) else {}
        cfg["categorias"][key]["act_indicacao"] = float(pd.to_numeric(pd.Series([src.get("act_indicacao", cfg["categorias"][key]["act_indicacao"])]), errors="coerce").fillna(0.0).iloc[0])
        for bucket in ["semana", "sabado"]:
            clean = []
            rows = src.get(bucket, cfg["categorias"][key][bucket])
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    min_v = int(pd.to_numeric(pd.Series([row.get("min")]), errors="coerce").fillna(0).iloc[0])
                    max_v = int(pd.to_numeric(pd.Series([row.get("max")]), errors="coerce").fillna(999).iloc[0])
                    valor = float(pd.to_numeric(pd.Series([row.get("valor")]), errors="coerce").fillna(0.0).iloc[0])
                    if min_v > 0 and max_v >= min_v:
                        clean.append({"min": min_v, "max": max_v, "valor": valor})
            cfg["categorias"][key][bucket] = sorted(clean or cfg["categorias"][key][bucket], key=lambda x: int(x["min"]))
    ops = raw.get("operadores") if isinstance(raw.get("operadores"), dict) else {}
    clean_ops = {}
    for k, v in ops.items():
        op_key = _normalize_person_key(k)
        if not op_key:
            continue
        if isinstance(v, dict):
            cat_raw = str(v.get("categoria") or v.get("category") or v.get("tipo") or "clt").strip().lower()
            if cat_raw not in {"clt", "estagiario", "outros"}:
                cat_raw = "clt"
            clean_ops[op_key] = {
                "categoria": cat_raw,
                "ativo": bool(v.get("ativo", True)),
                "relatorio": bool(v.get("relatorio", True)),
            }
        else:
            cat_raw = str(v or "clt").strip().lower()
            clean_ops[op_key] = _default_operator_settings("estagiario" if cat_raw == "estagiario" else ("outros" if cat_raw == "outros" else "clt"))
    cfg["operadores"] = clean_ops
    return cfg


def _load_c6_remun_config() -> dict:
    cfg = safe_json_load(C6_OP_REMUN_CFG, default=None)
    return _normalize_c6_remun_config(cfg)


def _save_c6_remun_config(cfg: dict):
    safe_json_save(C6_OP_REMUN_CFG, _normalize_c6_remun_config(cfg))


def _operator_category(operador: str, cfg: dict) -> str:
    key = _normalize_person_key(operador)
    item = (cfg.get("operadores") or {}).get(key)
    if isinstance(item, dict):
        return str(item.get("categoria") or "clt")
    if isinstance(item, str):
        return item
    return "clt"


def _operator_is_active(operador: str, cfg: dict) -> bool:
    key = _normalize_person_key(operador)
    item = (cfg.get("operadores") or {}).get(key)
    if isinstance(item, dict):
        return bool(item.get("ativo", True))
    return True


def _operator_in_email_reports(operador: str, cfg: dict) -> bool:
    key = _normalize_person_key(operador)
    item = (cfg.get("operadores") or {}).get(key)
    if isinstance(item, dict):
        return bool(item.get("relatorio", True))
    return True


def _operator_category_label(cat: str, cfg: dict) -> str:
    return ((cfg.get("categorias") or {}).get(cat) or {}).get("label", "Funcionários CLT")


def _tier_value_for_count(count: int, day_value, category_cfg: dict) -> float:
    day_ts = pd.to_datetime(day_value, errors="coerce")
    bucket = "sabado" if pd.notna(day_ts) and day_ts.weekday() == 5 else "semana"
    for row in category_cfg.get(bucket, []):
        if int(row.get("min", 0)) <= int(count) <= int(row.get("max", 999)):
            return float(row.get("valor", 0.0) or 0.0)
    return 0.0


def _c6_ops_signature(df_ops, df_leads, df_visao, ops_name: str, leads_name: str, visao_name: str) -> str:
    payload = {
        "ops": [ops_name, int(getattr(df_ops, "shape", [0, 0])[0]), int(getattr(df_ops, "shape", [0, 0])[1])],
        "leads": [leads_name, int(getattr(df_leads, "shape", [0, 0])[0]), int(getattr(df_leads, "shape", [0, 0])[1])],
        "visao": [visao_name, int(getattr(df_visao, "shape", [0, 0])[0]), int(getattr(df_visao, "shape", [0, 0])[1])],
    }
    return hashlib.md5(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _save_c6_remun_history(result: dict, signature: str):
    hist = safe_json_load(C6_OP_REMUN_HISTORY, default={}) or {}
    if hist.get("last_signature") == signature:
        return
    rows = []
    processed_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _add_from_df(df: pd.DataFrame, setor: str, operador_col: str, motivo_col: str, valor_col: str):
        if not isinstance(df, pd.DataFrame) or df.empty:
            return
        work = df.copy()
        if operador_col not in work.columns or valor_col not in work.columns:
            return
        work[valor_col] = pd.to_numeric(work[valor_col], errors="coerce").fillna(0.0)
        if motivo_col not in work.columns:
            work[motivo_col] = setor
        grp = (
            work.groupby([operador_col, motivo_col], dropna=False)
            .agg(quantidade=("cnpj", "nunique") if "cnpj" in work.columns else (valor_col, "size"), valor_total=(valor_col, "sum"))
            .reset_index()
        )
        for _, row in grp.iterrows():
            rows.append({
                "processado_em": processed_at,
                "setor": setor,
                "operador": str(row.get(operador_col, "") or ""),
                "motivo": str(row.get(motivo_col, "") or ""),
                "quantidade": int(row.get("quantidade", 0) or 0),
                "valor_total": float(row.get("valor_total", 0.0) or 0.0),
            })

    _add_from_df(result.get("act_report"), "ACT", "operador", "motivo_remuneracao", "valor_total")
    _add_from_df(result.get("oab_report"), "OCO", "operador", "motivo_remuneracao", "valor_total")
    _add_from_df(result.get("omc_report"), "OQL", "operador", "motivo_remuneracao", "valor_real_agora")
    hist["last_signature"] = signature
    hist["updated_at"] = processed_at
    hist["rows"] = (hist.get("rows") or []) + rows
    hist["rows"] = hist["rows"][-5000:]
    safe_json_save(C6_OP_REMUN_HISTORY, hist)


def _process_c6_operacao(df_ops_raw: pd.DataFrame, df_leads_raw: pd.DataFrame, df_visao_raw: pd.DataFrame, persist_history: bool = False) -> Dict[str, pd.DataFrame]:
    hist = _load_c6_ops_history()
    remun_cfg = _load_c6_remun_config()
    imports_log = hist["imports"] if isinstance(hist["imports"], list) else []
    pix_track = hist["pix_track"] if isinstance(hist["pix_track"], dict) else {}
    omc_maxpay = hist["omc_maxpay"] if isinstance(hist["omc_maxpay"], dict) else {}

    ops = _extract_ops_base(df_ops_raw)
    leads = _extract_leads_base(df_leads_raw)
    visao = _extract_visao_base(df_visao_raw)
    if not ops.empty and "operador" in ops.columns:
        ops["operador"] = _canonicalize_operator_series(ops["operador"], remun_cfg)

    base = leads.merge(visao, on="cnpj", how="outer", suffixes=("_lead", "_visao"))
    base["nome_cliente"] = base["nome_cliente_lead"].fillna("").replace("", pd.NA).fillna(base["nome_cliente_visao"])
    base["data_base"] = base.get("data_base_lead", pd.Series(pd.NaT, index=base.index)).fillna(base.get("data_base_visao", pd.Series(pd.NaT, index=base.index)))
    base["dt_fundacao_empresa"] = base.get("dt_fundacao_empresa", pd.Series(pd.NaT, index=base.index))
    base["status_abertura_conta"] = base.get("status_abertura_conta", pd.Series("", index=base.index)).fillna("")
    act = _merge_action_latest(ops, "ACT", base, "data_hora_cadastro")
    oco = _merge_action_latest(ops, "OCO", base, "dt_conta_criada")
    oql = _merge_action_latest(ops, "OQL", visao, "data_base")

    act_rep = base.merge(act[["cnpj", "operador", "data_acao"]], on="cnpj", how="left")
    for col in ["data_acao", "data_hora_cadastro", "dt_conta_criada", "dt_fundacao_empresa", "data_base"]:
        if col in act_rep.columns:
            act_rep[col] = pd.to_datetime(act_rep[col], errors="coerce")
    act_rep["janela_14d"] = act_rep.apply(lambda r: pd.notna(r.get("dt_conta_criada")) and pd.notna(r.get("data_hora_cadastro")) and 0 <= _days_between(r.get("data_hora_cadastro"), r.get("dt_conta_criada")) <= 14, axis=1)
    act_rep["abriu_conta"] = act_rep["dt_conta_criada"].notna()
    act_rep["abriu_apos_indicacao"] = act_rep.apply(
        lambda r: pd.notna(r.get("dt_conta_criada")) and pd.notna(r.get("data_acao")) and pd.Timestamp(r.get("dt_conta_criada")).normalize() >= pd.Timestamp(r.get("data_acao")).normalize(),
        axis=1,
    )
    act_rep["janela_14d"] = act_rep["janela_14d"] & act_rep["abriu_apos_indicacao"]
    act_rep["categoria_operador"] = act_rep["operador"].apply(lambda x: _operator_category(x, remun_cfg))
    act_rep["tipo_operador"] = act_rep["categoria_operador"].apply(lambda x: _operator_category_label(x, remun_cfg))
    act_rep["operador_ativo"] = act_rep["operador"].apply(lambda x: _operator_is_active(x, remun_cfg))
    act_rep["operador_relatorio"] = act_rep["operador"].apply(lambda x: _operator_in_email_reports(x, remun_cfg))
    act_rep["dia_comissao"] = pd.to_datetime(act_rep["dt_conta_criada"], errors="coerce").dt.date
    act_rep["dia_comissao_key"] = pd.to_datetime(act_rep["dia_comissao"], errors="coerce").dt.strftime("%Y-%m-%d").fillna("")
    act_rep["mes_ref_comissao"] = pd.to_datetime(act_rep["dia_comissao"], errors="coerce").dt.strftime("%Y-%m").fillna("")
    act_rep["qtd_aberturas_dia_operador"] = 0
    act_rep["valor_unitario"] = 0.0
    valid_act_df = act_rep[
        act_rep["abriu_apos_indicacao"]
        & act_rep["operador_ativo"]
        & act_rep["dia_comissao_key"].ne("")
        & act_rep["categoria_operador"].ne("outros")
    ].copy()
    if not valid_act_df.empty:
        day_counts = valid_act_df.groupby(["operador", "dia_comissao_key"]).size().to_dict()
        for row_idx, row in valid_act_df.iterrows():
            count_day = int(day_counts.get((row.get("operador"), row.get("dia_comissao_key")), 0) or 0)
            cat_cfg = remun_cfg["categorias"].get(row.get("categoria_operador")) or remun_cfg["categorias"]["clt"]
            unit_value = _tier_value_for_count(count_day, row.get("dia_comissao"), cat_cfg)
            act_rep.at[row_idx, "qtd_aberturas_dia_operador"] = count_day
            act_rep.at[row_idx, "valor_unitario"] = unit_value
    act_rep["valor_indicacao"] = act_rep["valor_unitario"]
    act_rep["valor_bonus"] = 0.0
    act_rep["valor_total"] = act_rep["valor_indicacao"] + act_rep["valor_bonus"]
    act_rep["motivo_remuneracao"] = "Indicação ACT"
    act_rep["faixa_idade_empresa"] = act_rep.apply(lambda r: _faixa_idade_empresa(r.get("dt_fundacao_empresa"), r.get("data_base")), axis=1)
    act_rep["dias_ate_abertura"] = act_rep.apply(lambda r: _days_between(r.get("data_hora_cadastro"), r.get("dt_conta_criada")), axis=1)
    act_rep["mes_ref"] = act_rep["data_hora_cadastro"].dt.strftime("%Y-%m").fillna("")
    act_rep = act_rep[act_rep["operador"].fillna("").astype(str).str.strip().ne("")].copy()
    act_cols = [
        "operador", "tipo_operador", "categoria_operador", "operador_ativo", "operador_relatorio",
        "nome_cliente", "cnpj", "status_abertura_conta", "status_final", "pendencias",
        "data_acao", "data_hora_cadastro", "dt_conta_criada", "dia_comissao", "mes_ref_comissao",
        "dt_fundacao_empresa", "faixa_idade_empresa", "dias_ate_abertura", "abriu_apos_indicacao",
        "janela_14d", "qtd_aberturas_dia_operador", "motivo_remuneracao", "valor_unitario",
        "valor_indicacao", "valor_bonus", "valor_total", "mes_ref"
    ]
    for col in ["status_abertura_conta", "status_final", "pendencias"]:
        if col not in act_rep.columns:
            act_rep[col] = ""
    act_rep = act_rep[act_cols].sort_values(["operador", "data_acao", "nome_cliente"], na_position="last")
    act_oper_base = act_rep[act_rep["operador_ativo"]].copy()
    act_oper = act_oper_base.groupby(["operador", "tipo_operador"], dropna=False).agg(clientes_indicados=("cnpj", "nunique"), contas_abertas=("abriu_apos_indicacao", "sum"), abertas_14d=("janela_14d", "sum"), comissao_total=("valor_total", "sum"), aparece_email=("operador_relatorio", "max")).reset_index()
    act_oper["eficiencia_%"] = (act_oper["contas_abertas"] / act_oper["clientes_indicados"].replace(0, pd.NA) * 100).fillna(0).round(2)
    act_conversao_operadores = act_oper.rename(columns={
        "clientes_indicados": "clientes_act",
        "contas_abertas": "clientes_abriram_conta",
        "abertas_14d": "clientes_abriram_14d",
        "eficiencia_%": "conversao_%"
    })[["operador", "tipo_operador", "clientes_act", "clientes_abriram_conta", "clientes_abriram_14d", "conversao_%", "comissao_total", "aparece_email"]].copy()
    act_conversao_operadores = act_conversao_operadores.sort_values(["conversao_%", "clientes_abriram_conta", "clientes_act"], ascending=[False, False, False])
    act_daily_base = act_rep[act_rep["operador_ativo"] & act_rep["dia_comissao"].notna()].copy()
    act_diario = act_daily_base.groupby(["dia_comissao", "operador", "tipo_operador"], dropna=False).agg(clientes_indicados=("cnpj", "nunique"), contas_abertas=("abriu_apos_indicacao", "sum"), abertas_14d=("janela_14d", "sum"), comissao_total=("valor_total", "sum"), aparece_email=("operador_relatorio", "max")).reset_index()
    if not act_diario.empty:
        act_diario["eficiencia_%"] = (act_diario["contas_abertas"] / act_diario["clientes_indicados"].replace(0, pd.NA) * 100).fillna(0).round(2)
        act_diario = act_diario.sort_values(["dia_comissao", "comissao_total", "contas_abertas"], ascending=[False, False, False])
    act_mensal = act_daily_base[act_daily_base["mes_ref_comissao"].ne("")].groupby(["mes_ref_comissao", "operador", "tipo_operador"], dropna=False).agg(clientes_indicados=("cnpj", "nunique"), contas_abertas=("abriu_apos_indicacao", "sum"), abertas_14d=("janela_14d", "sum"), comissao_total=("valor_total", "sum"), aparece_email=("operador_relatorio", "max")).reset_index()
    if not act_mensal.empty:
        act_mensal["eficiencia_%"] = (act_mensal["contas_abertas"] / act_mensal["clientes_indicados"].replace(0, pd.NA) * 100).fillna(0).round(2)
        act_mensal = act_mensal.sort_values(["mes_ref_comissao", "comissao_total", "contas_abertas"], ascending=[False, False, False])
    latest_act_day = act_daily_base.loc[act_daily_base["abriu_apos_indicacao"], "dia_comissao"].max() if not act_daily_base.empty else pd.NaT
    act_diario_atual = act_diario[act_diario["dia_comissao"].eq(latest_act_day)].copy() if pd.notna(latest_act_day) and not act_diario.empty else pd.DataFrame()
    act_faixa = act_rep[act_rep["dt_fundacao_empresa"].notna()].groupby("faixa_idade_empresa", dropna=False).agg(clientes=("cnpj", "nunique"), abertas=("abriu_apos_indicacao", "sum")).reset_index()
    act_faixa["taxa_abertura_%"] = (act_faixa["abertas"] / act_faixa["clientes"].replace(0, pd.NA) * 100).fillna(0).round(2)

    oab_rep = base.merge(oco[["cnpj", "operador", "data_acao"]], on="cnpj", how="left")
    for col in ["data_acao", "data_hora_cadastro", "dt_conta_criada", "data_base"]:
        if col in oab_rep.columns:
            oab_rep[col] = pd.to_datetime(oab_rep[col], errors="coerce")
    oab_rep["dias_ate_abertura"] = oab_rep.apply(lambda r: _days_between(r.get("data_hora_cadastro"), r.get("dt_conta_criada")), axis=1)
    oab_rep["faixa_abertura"] = oab_rep["dias_ate_abertura"].apply(_faixa_abertura)
    oab_rep["abriu_apos_acao"] = oab_rep.apply(
        lambda r: pd.notna(r.get("dt_conta_criada")) and pd.notna(r.get("data_acao")) and pd.Timestamp(r.get("dt_conta_criada")).normalize() >= pd.Timestamp(r.get("data_acao")).normalize(),
        axis=1,
    )
    oab_rep["dias_uteis_bko"] = oab_rep.apply(
        lambda r: _calc_bko_business_streak(r.get("cnpj"), r.get("data_base"))
        if _normalize_status_key(r.get("status_abertura_conta", "")) == "AGUARDAR ATUACAO MANUAL BKO" else None,
        axis=1
    )
    oab_rep["mes_ref"] = oab_rep["data_hora_cadastro"].dt.strftime("%Y-%m").fillna("")
    oab_rep["valor_unitario"] = 0.0
    oab_rep["categoria_operador"] = oab_rep["operador"].apply(lambda x: _operator_category(x, remun_cfg))
    oab_rep["tipo_operador"] = oab_rep["categoria_operador"].apply(lambda x: _operator_category_label(x, remun_cfg))
    oab_rep["dia_meta"] = oab_rep["data_acao"]
    oab_rep.loc[oab_rep["dia_meta"].isna(), "dia_meta"] = oab_rep.loc[oab_rep["dia_meta"].isna(), "dt_conta_criada"]
    oab_rep["dia_meta_key"] = pd.to_datetime(oab_rep["dia_meta"], errors="coerce").dt.strftime("%Y-%m-%d").fillna("")
    valid_df = oab_rep[oab_rep["abriu_apos_acao"] & oab_rep["dia_meta_key"].ne("")].copy()
    if not valid_df.empty:
        day_counts = valid_df.groupby(["operador", "dia_meta_key"]).size().to_dict()
        for row_idx, row in valid_df.iterrows():
            count_day = int(day_counts.get((row.get("operador"), row.get("dia_meta_key")), 0) or 0)
            cat_cfg = remun_cfg["categorias"].get(row.get("categoria_operador")) or remun_cfg["categorias"]["clt"]
            oab_rep.at[row_idx, "valor_unitario"] = _tier_value_for_count(count_day, row.get("dia_meta"), cat_cfg)
            oab_rep.at[row_idx, "qtd_contas_dia_operador"] = count_day
            oab_rep.at[row_idx, "motivo_remuneracao"] = "Conta aberta OCO"
    if "qtd_contas_dia_operador" not in oab_rep.columns:
        oab_rep["qtd_contas_dia_operador"] = 0
    oab_rep["qtd_contas_dia_operador"] = pd.to_numeric(oab_rep["qtd_contas_dia_operador"], errors="coerce").fillna(0).astype(int)
    if "motivo_remuneracao" not in oab_rep.columns:
        oab_rep["motivo_remuneracao"] = ""
    oab_rep["motivo_remuneracao"] = oab_rep["motivo_remuneracao"].fillna("")
    oab_rep["valor_total"] = oab_rep["valor_unitario"]
    bko_alerta = _build_bko_followup_table(base, oco)
    bko_status_summary = (
        bko_alerta.groupby("status_bko", dropna=False)
        .size()
        .reset_index(name="quantidade")
        .rename(columns={"status_bko": "status"})
        if not bko_alerta.empty else pd.DataFrame({"status": ["Ativo", "Inativo"], "quantidade": [0, 0]})
    )

    bko_current = oab_rep[oab_rep["status_abertura_conta"].apply(_normalize_status_key).eq("AGUARDAR ATUACAO MANUAL BKO")].copy()
    if not bko_current.empty:
        def _bucket_bko(x):
            if x is None: return ""
            if x <= 1: return "1 dia útil"
            if x == 2: return "2 dias úteis"
            if x == 3: return "3 dias úteis"
            if x == 4: return "4 dias úteis"
            return "5+ dias úteis"
        bko_current["bucket_bko"] = bko_current["dias_uteis_bko"].fillna(0).astype(int).apply(_bucket_bko)
        bko_sum = bko_current.groupby("bucket_bko").size().reset_index(name="quantidade").rename(columns={"bucket_bko": "faixa"})
        bko_sum = bko_sum.set_index("faixa").reindex(["1 dia útil","2 dias úteis","3 dias úteis","4 dias úteis","5+ dias úteis"], fill_value=0).reset_index()
    else:
        bko_sum = pd.DataFrame({"faixa": ["1 dia útil","2 dias úteis","3 dias úteis","4 dias úteis","5+ dias úteis"], "quantidade": [0,0,0,0,0]})
    oab_rep = oab_rep[oab_rep["operador"].fillna("").astype(str).str.strip().ne("")].copy()
    oab_screen = oab_rep[["operador", "tipo_operador", "nome_cliente", "cnpj", "data_acao", "data_hora_cadastro", "dt_conta_criada", "dia_meta", "qtd_contas_dia_operador", "dias_ate_abertura", "faixa_abertura", "status_abertura_conta", "dias_uteis_bko", "abriu_apos_acao", "motivo_remuneracao", "valor_unitario", "valor_total", "mes_ref"]].sort_values(["operador", "data_acao", "dt_conta_criada"], na_position="last")
    oab_oper = oab_rep.groupby(["operador", "tipo_operador"], dropna=False).agg(clientes_trabalhados=("cnpj", "nunique"), contas_validas=("abriu_apos_acao", "sum"), comissao_total=("valor_total", "sum")).reset_index()
    total_indicados_mes = int(leads["cnpj"].nunique())
    oab_oper["indicados_mes_base"] = total_indicados_mes
    oab_oper["eficiencia_vs_indicados_%"] = (oab_oper["contas_validas"] / oab_oper["indicados_mes_base"].replace(0, pd.NA) * 100).fillna(0).round(2)

    omc_rep = visao.merge(oql[["cnpj", "operador", "data_acao"]], on="cnpj", how="left")
    if "pix" in omc_rep.columns:
        omc_rep["pix"] = omc_rep["pix"].apply(lambda x: _pix_clean_value(x) if _pix_is_valid(x) else "")
    if "pix_tipo" in omc_rep.columns:
        omc_rep["pix_tipo"] = omc_rep["pix"]
    for col in ["data_acao", "data_base", "dt_conta_criada", "dt_aprovacao_pay", "dt_install_maq", "dt_ativacao_pay", "dt_entrega_cartao", "dt_ativ_cartao_cred"]:
        if col in omc_rep.columns:
            omc_rep[col] = pd.to_datetime(omc_rep[col], errors="coerce")
    omc_rep["acao_mes"] = omc_rep["data_acao"].dt.strftime("%Y-%m").fillna("")
    omc_rep["mes_base"] = omc_rep["data_base"].dt.strftime("%Y-%m").fillna("")
    omc_rep["acao_valida_mes"] = omc_rep["acao_mes"].eq(omc_rep["mes_base"])
    omc_rep["qualificado_valido"] = omc_rep["qualificado"] & omc_rep["acao_valida_mes"]
    omc_rep = omc_rep[omc_rep["operador"].fillna("").astype(str).str.strip().ne("")].copy()
    qtd_qual_op = omc_rep[omc_rep["qualificado_valido"]].groupby("operador")["cnpj"].nunique().to_dict()
    omc_rep["qtd_qual_op_mes"] = omc_rep["operador"].map(qtd_qual_op).fillna(0).astype(int)
    omc_rep["valor_teorico"] = omc_rep.apply(lambda r: ((5.0 if r["qtd_qual_op_mes"] > 100 else 3.0) + (10.0 if r["nivel_maximo"] >= 4 else 0.0)) if r["qualificado_valido"] else 0.0, axis=1)

    def _omc_paid_before_current(cnpj, mes_base):
        info = omc_maxpay.get(cnpj) or {}
        paid_month = str(info.get("month", "") or "")
        current_month = str(mes_base or "")
        if paid_month and current_month and paid_month < current_month:
            return float(info.get("max_paid", 0.0))
        return 0.0

    def _omc_paid_month_before_current(cnpj, mes_base):
        info = omc_maxpay.get(cnpj) or {}
        paid_month = str(info.get("month", "") or "")
        current_month = str(mes_base or "")
        if paid_month and current_month and paid_month < current_month:
            return paid_month
        return ""

    omc_rep["valor_ja_pago"] = omc_rep.apply(lambda r: _omc_paid_before_current(r["cnpj"], r["mes_base"]), axis=1)
    omc_rep["valor_teorico"] = pd.to_numeric(omc_rep["valor_teorico"], errors="coerce").fillna(0.0)
    omc_rep["valor_ja_pago"] = pd.to_numeric(omc_rep["valor_ja_pago"], errors="coerce").fillna(0.0)
    omc_rep["mes_ja_pago"] = omc_rep.apply(lambda r: _omc_paid_month_before_current(r["cnpj"], r["mes_base"]), axis=1)
    omc_rep["valor_real_agora"] = (omc_rep["valor_teorico"] - omc_rep["valor_ja_pago"]).clip(lower=0)
    omc_rep["categoria_operador"] = omc_rep["operador"].apply(lambda x: _operator_category(x, remun_cfg))
    omc_rep["tipo_operador"] = omc_rep["categoria_operador"].apply(lambda x: _operator_category_label(x, remun_cfg))
    omc_rep["motivo_remuneracao"] = omc_rep.apply(
        lambda r: "Qualificação OQL nível 4" if bool(r.get("qualificado_valido")) and float(r.get("valor_real_agora", 0) or 0) > 0 and int(r.get("nivel_maximo", 0) or 0) >= 4
        else ("Qualificação OQL" if bool(r.get("qualificado_valido")) and float(r.get("valor_real_agora", 0) or 0) > 0 else ""),
        axis=1,
    )
    for _, row in omc_rep.iterrows():
        cnpj = row["cnpj"]
        if not cnpj:
            continue
        hist_pix = pix_track.get(cnpj, {})
        atual_tem_pix = bool(row["tem_pix"])
        data_base = pd.Timestamp(row["data_base"]).date().isoformat() if pd.notna(row["data_base"]) else ""
        pix_tipo = row.get("pix_tipo", "")
        if atual_tem_pix and not hist_pix.get("first_seen"):
            hist_pix["first_seen"] = data_base
            hist_pix["first_tipo"] = pix_tipo
            hist_pix["first_operator"] = row.get("operador") if str(row.get("operador"," ")).strip() else "sem operador"
        if atual_tem_pix:
            hist_pix["current_has_pix"] = True
            hist_pix["last_seen"] = data_base
            hist_pix["removed_on"] = ""
        elif hist_pix.get("current_has_pix"):
            hist_pix["current_has_pix"] = False
            hist_pix["removed_on"] = data_base
        pix_track[cnpj] = hist_pix
    omc_rep["pix_primeira_aparicao"] = omc_rep["cnpj"].apply(lambda c: (pix_track.get(c) or {}).get("first_seen", ""))
    omc_rep["pix_operador_origem"] = omc_rep["cnpj"].apply(lambda c: (pix_track.get(c) or {}).get("first_operator", "sem operador"))
    omc_rep["pix_retirado_em"] = omc_rep["cnpj"].apply(lambda c: (pix_track.get(c) or {}).get("removed_on", ""))
    omc_screen = omc_rep[omc_rep["acao_valida_mes"]].copy()
    omc_screen = omc_screen[["operador", "tipo_operador", "nome_cliente", "cnpj", "data_acao", "data_base", "dt_conta_criada", "estagio_m", "criterios_atingidos_comiss", "nivel_maximo", "qualificado_valido", "motivo_remuneracao", "valor_teorico", "valor_ja_pago", "mes_ja_pago", "valor_real_agora", "pix", "pix_tipo", "pix_primeira_aparicao", "pix_operador_origem", "pix_retirado_em", "wallet", "fl_propensao_c6pay", "tpv_c6pay_potencial", "fl_elegivel_venda_c6pay", "status_proposta_sf_pay", "dt_aprovacao_pay", "dt_install_maq", "dt_ativacao_pay", "c6pay_ativa_30", "dt_ult_trans_pay", "vl_cash_in_mtd", "banco_domicilio", "acao_valida_mes"]].sort_values(["operador", "valor_real_agora", "nome_cliente"], ascending=[True, False, True], na_position="last")
    omc_oper = omc_screen.groupby(["operador", "tipo_operador"], dropna=False).agg(clientes_base=("cnpj", "nunique"), qualificados=("qualificado_valido", "sum"), nivel4=("nivel_maximo", lambda s: int((pd.to_numeric(s, errors="coerce").fillna(0) >= 4).sum())), valor_teorico_total=("valor_teorico", "sum"), valor_real_total=("valor_real_agora", "sum")).reset_index()
    for est in ["M0","M1","M2"]:
        counts = omc_screen[omc_screen["qualificado_valido"] & omc_screen["estagio_m"].eq(est)].groupby("operador")["cnpj"].nunique()
        omc_oper[f"{est}_qualificados"] = omc_oper["operador"].map(counts).fillna(0).astype(int)
    omc_oper["eficiencia_qualificacao_%"] = (omc_oper["qualificados"] / omc_oper["clientes_base"].replace(0, pd.NA) * 100).fillna(0).round(2)

    resumo = pd.DataFrame([
        {"Indicador": "Indicadores ACT", "Valor": int(act_rep["operador"].fillna("").ne("").sum())},
        {"Indicador": "Conversões 14 dias", "Valor": int(act_rep["janela_14d"].sum())},
        {"Indicador": "Clientes BKO 5+ dias úteis", "Valor": int((bko_alerta["dias_uteis_bko"].fillna(0) >= 5).sum()) if not bko_alerta.empty else 0},
        {"Indicador": "Qualificados válidos OMC", "Valor": int(omc_screen["qualificado_valido"].sum())},
    ])

    if persist_history:
        imports_log.append({"processed_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "ops_rows": int(len(ops)), "leads_rows": int(len(leads)), "visao_rows": int(len(visao))})
        for _, row in omc_rep[omc_rep["valor_real_agora"] > 0].iterrows():
            omc_maxpay[row["cnpj"]] = {"max_paid": float(max(float(row["valor_ja_pago"]), float(row["valor_teorico"]))), "month": row["mes_base"], "operador": row.get("operador", "")}
        _save_c6_ops_history(imports_log, pix_track, omc_maxpay)

    return {"act_report": act_rep, "act_operadores": act_oper.sort_values(["comissao_total", "abertas_14d"], ascending=False), "act_conversao_operadores": act_conversao_operadores, "act_diario": act_diario, "act_diario_atual": act_diario_atual, "act_mensal": act_mensal, "act_faixa": act_faixa, "oab_report": oab_screen, "oab_operadores": oab_oper.sort_values(["comissao_total", "contas_validas"], ascending=False), "bko_alerta": bko_alerta, "bko_summary": bko_sum, "bko_status_summary": bko_status_summary, "omc_report": omc_screen, "omc_operadores": omc_oper.sort_values(["valor_real_total", "qualificados"], ascending=False), "resumo": resumo}


def _latest_c6_operacao_result_for_reports() -> dict:
    cached = st.session_state.get("c6_operacao_last_result")
    if isinstance(cached, dict) and cached:
        return cached
    df_ops_raw, _ = _load_ops_import_cache()
    df_leads_raw, _, _ = _load_daily_import_cache("leads")
    df_visao_raw, _, _ = _load_daily_import_cache("visao")
    if df_ops_raw is None or df_leads_raw is None or df_visao_raw is None:
        return {}
    try:
        result = _process_c6_operacao(df_ops_raw, df_leads_raw, df_visao_raw, persist_history=False)
        st.session_state["c6_operacao_last_result"] = result
        st.session_state["c6_operacao_last_result__ts"] = dt.datetime.now().timestamp()
        return result
    except Exception:
        return {}


def _filter_act_email_rows(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty or "operador" not in df.columns:
        return df
    cfg = _load_c6_remun_config()
    out = df.copy()
    if "aparece_email" in out.columns:
        return out[out["aparece_email"].astype(bool)].copy()
    if "operador_relatorio" in out.columns:
        return out[out["operador_relatorio"].astype(bool)].copy()
    out["_relatorio"] = out["operador"].apply(lambda x: _operator_in_email_reports(x, cfg))
    out = out[out["_relatorio"]].drop(columns=["_relatorio"])
    return out


def _render_metric_cards(items: List[tuple]):
    cols = st.columns(len(items))
    for col, (label, value, delta) in zip(cols, items):
        with col:
            st.metric(label, value, delta)


def _format_report_df(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    int_like = [
        "clientes_indicados", "contas_abertas", "abertas_14d", "clientes", "abertas",
        "clientes_trabalhados", "contas_validas", "indicados_mes_base", "clientes_base",
        "qualificados", "nivel4", "M0_qualificados", "M1_qualificados", "M2_qualificados",
        "dias_ate_abertura", "dias_uteis_bko", "qtd_qual_op_mes", "quantidade", "qtd_contas_dia_operador",
        "qtd_aberturas_dia_operador", "clientes_act", "clientes_abriram_conta", "clientes_abriram_14d"
    ]
    money_like = [
        "comissao_total", "valor_indicacao", "valor_bonus", "valor_total",
        "valor_unitario", "valor_teorico", "valor_ja_pago", "valor_real_agora",
        "valor_teorico_total", "valor_real_total"
    ]
    pct_like = ["eficiencia_%", "taxa_abertura_%", "eficiencia_vs_indicados_%", "eficiencia_qualificacao_%", "conversao_%"]

    for col in out.columns:
        if col in int_like:
            out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0).astype(int).apply(br_int)
        elif col in money_like:
            out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0.0).apply(br_money)
        elif col in pct_like:
            out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0.0).apply(lambda x: f"{x:.1f}%".replace(".", ","))
        elif str(out[col].dtype).startswith("datetime64"):
            out[col] = pd.to_datetime(out[col], errors="coerce").dt.strftime("%d/%m/%Y %H:%M")
    return out

def _filter_c6_result_for_operator(result: dict, operator_filter: str) -> dict:
    filt = _normalize_person_key(operator_filter)
    if not filt:
        return result
    out = dict(result)
    for k in ["act_report", "act_operadores", "act_conversao_operadores", "act_diario", "act_diario_atual", "act_mensal", "oab_report", "oab_operadores", "omc_report", "omc_operadores", "bko_alerta"]:
        df = out.get(k)
        if isinstance(df, pd.DataFrame) and not df.empty and "operador" in df.columns:
            tmp = df.copy()
            tmp["_op_key"] = tmp["operador"].apply(_normalize_person_key)
            tmp = tmp[tmp["_op_key"].eq(filt)].drop(columns=["_op_key"])
            out[k] = tmp
    if isinstance(out.get("bko_summary"), pd.DataFrame):
        out["bko_summary"] = out["bko_summary"].copy()
    if isinstance(out.get("act_faixa"), pd.DataFrame):
        out["act_faixa"] = pd.DataFrame()
    return out


def _render_c6_remun_config(result: dict, view_only: bool = False):
    if view_only:
        return
    cfg = _load_c6_remun_config()
    operators = set()
    for key in ["act_report", "oab_report", "omc_report"]:
        df = result.get(key)
        if isinstance(df, pd.DataFrame) and not df.empty and "operador" in df.columns:
            operators.update(str(v).strip() for v in df["operador"].dropna().tolist() if str(v).strip())
    operators.update((cfg.get("operadores") or {}).keys())
    with st.expander("Configurar remuneração dos operadores", expanded=False):
        tab_clt, tab_est, tab_ops = st.tabs(["Funcionários CLT", "Estagiários", "Operadores"])

        def _rules_editor(tab, cat_key: str):
            cat = cfg["categorias"][cat_key]
            with tab:
                st.markdown("**Ação ACT - meta diária por operador**")
                st.markdown("**Conta aberta OCO - segunda a sexta**")
                week_rules = []
                cols = st.columns(3)
                for idx, rule in enumerate(cat.get("semana", [])[:3], start=1):
                    with cols[idx - 1]:
                        min_v = st.number_input(f"Faixa {idx} mín.", min_value=1, step=1, value=int(rule.get("min", 1)), key=f"c6_remun_{cat_key}_sem_min_{idx}")
                        max_v = st.number_input(f"Faixa {idx} máx.", min_value=min_v, step=1, value=int(rule.get("max", 999)), key=f"c6_remun_{cat_key}_sem_max_{idx}")
                        valor = st.number_input(f"Faixa {idx} valor", min_value=0.0, step=0.50, value=float(rule.get("valor", 0.0)), key=f"c6_remun_{cat_key}_sem_val_{idx}")
                        week_rules.append({"min": int(min_v), "max": int(max_v), "valor": float(valor)})
                st.markdown("**Conta aberta OCO - sábado**")
                sat_rules = []
                cols = st.columns(3)
                for idx, rule in enumerate(cat.get("sabado", [])[:3], start=1):
                    with cols[idx - 1]:
                        min_v = st.number_input(f"Sábado faixa {idx} mín.", min_value=1, step=1, value=int(rule.get("min", 1)), key=f"c6_remun_{cat_key}_sab_min_{idx}")
                        max_v = st.number_input(f"Sábado faixa {idx} máx.", min_value=min_v, step=1, value=int(rule.get("max", 999)), key=f"c6_remun_{cat_key}_sab_max_{idx}")
                        valor = st.number_input(f"Sábado faixa {idx} valor", min_value=0.0, step=0.50, value=float(rule.get("valor", 0.0)), key=f"c6_remun_{cat_key}_sab_val_{idx}")
                        sat_rules.append({"min": int(min_v), "max": int(max_v), "valor": float(valor)})
                return {"act_indicacao": 0.0, "semana": week_rules, "sabado": sat_rules}

        clt_rules = _rules_editor(tab_clt, "clt")
        est_rules = _rules_editor(tab_est, "estagiario")

        with tab_ops:
            st.caption("Operadores sem classificação ficam como Funcionários CLT.")
            op_map = dict(cfg.get("operadores") or {})
            new_op = st.text_input("Adicionar operador manualmente", value="", key="c6_remun_new_operator")
            if str(new_op or "").strip():
                operators.add(str(new_op).strip())
            # Uma entrada por nome normalizado — evita StreamlitDuplicateElementKey quando o
            # mesmo operador aparece com grafias diferentes nas bases (ACT/OCO/OQL).
            op_label_by_norm: Dict[str, str] = {}
            for raw in operators:
                nk = _normalize_person_key(raw)
                if not nk:
                    continue
                label = str(raw).strip()
                prev = op_label_by_norm.get(nk)
                if prev is None or len(label) > len(prev):
                    op_label_by_norm[nk] = label
            for op_key in sorted(op_label_by_norm.keys()):
                op = op_label_by_norm[op_key]
                current = op_map.get(op_key, _default_operator_settings("clt"))
                if not isinstance(current, dict):
                    current = _default_operator_settings(current)
                cols = st.columns([2.2, 1.2, 0.8, 1.0])
                cols[0].write(op)
                choice = cols[1].selectbox(
                    "Categoria",
                    ["clt", "estagiario", "outros"],
                    index=["clt", "estagiario", "outros"].index(str(current.get("categoria", "clt")) if str(current.get("categoria", "clt")) in ["clt", "estagiario", "outros"] else "clt"),
                    format_func=lambda x: "Funcionário CLT" if x == "clt" else ("Estagiário" if x == "estagiario" else "Outros"),
                    key=f"c6_remun_op_cat_{op_key}",
                )
                active = cols[2].checkbox("Ativo", value=bool(current.get("ativo", True)), key=f"c6_remun_op_active_{op_key}")
                in_report = cols[3].checkbox("Relatório/e-mail", value=bool(current.get("relatorio", True)), key=f"c6_remun_op_report_{op_key}")
                op_map[op_key] = {"categoria": choice, "ativo": bool(active), "relatorio": bool(in_report)}

        if st.button("Salvar configuração de remuneração", key="c6_remun_save_btn", type="primary"):
            cfg["categorias"]["clt"].update(clt_rules)
            cfg["categorias"]["estagiario"].update(est_rules)
            cfg["operadores"] = op_map
            _save_c6_remun_config(cfg)
            st.success("Configuração de remuneração salva. O cálculo será atualizado automaticamente.")
            st.rerun()


def _render_c6_remun_history():
    hist = safe_json_load(C6_OP_REMUN_HISTORY, default={}) or {}
    rows = hist.get("rows") if isinstance(hist.get("rows"), list) else []
    if not rows:
        return
    with st.expander("Histórico de remuneração por setor e motivo", expanded=False):
        df_hist = pd.DataFrame(rows)
        if df_hist.empty:
            return
        render_downloadable_table(
            _format_report_df(df_hist),
            "c6_remun_hist",
            "c6_remuneracao_historico",
            raw_df=df_hist,
        )


def _render_c6_operacao_tab(view_only: bool = False, operator_filter: str = ""):
    st.subheader("C6 Operação")
    up_ops = None
    if not view_only:
        u1 = st.columns(1)[0]
        with u1:
            up_ops = st.file_uploader("XPrisma / Grelacd (.csv ou .xlsx)", type=["csv", "xlsx"], key="c6_operacao_ops")
    up_leads = None
    up_visao = None
    if up_ops:
        raw_ops_bytes = up_ops.getvalue()
        df_ops_upload = _read_ops_file(up_ops)
        if df_ops_upload is not None and not df_ops_upload.empty:
            st.session_state["c6_operacao_ops_df"] = df_ops_upload
            st.session_state["c6_operacao_ops_df__name"] = up_ops.name
            st.session_state["c6_operacao_ops_df__ts"] = dt.datetime.now().timestamp()
            _save_ops_import_cache(up_ops.name, raw_ops_bytes)
            _clear_c6_operacao_runtime_cache()
            if "firebase" not in st.secrets:
                _ops_sync_sig = json.dumps(["ops", up_ops.name, getattr(up_ops, "size", 0)], ensure_ascii=False)
                if st.session_state.get("_last_ops_cloud_sync_sig") != _ops_sync_sig:
                    with st.spinner("Sincronizando C6 Operação com o app online..."):
                        _sync_ok, _sync_msg = _sync_local_data_to_cloud_seed("c6-operacao-upload")
                    st.session_state["_last_ops_cloud_sync_sig"] = _ops_sync_sig
                    if _sync_ok:
                        st.success("C6 Operação publicado para o app online. O Streamlit pode levar alguns minutos para recarregar.")
                    elif "Sem mudanças" not in _sync_msg:
                        st.warning(_sync_msg)
        else:
            st.warning("Arquivo de operadores veio vazio ou não foi reconhecido; mantive o último cache válido.")

    def _pick_latest_session_df(options):
        best_df = None
        best_name = ""
        best_origin = ""
        best_ts = -1.0
        for key, origin in options:
            df_obj = st.session_state.get(key)
            ts = float(st.session_state.get(f"{key}__ts", -1.0) or -1.0)
            if df_obj is not None and ts >= best_ts:
                best_df = df_obj
                best_name = str(st.session_state.get(f"{key}__name", "") or "")
                best_origin = origin
                best_ts = ts
        return best_df, best_name, best_origin

    df_ops_raw = st.session_state.get("c6_operacao_ops_df")
    ops_name = str(st.session_state.get("c6_operacao_ops_df__name", "") or "")
    df_leads_raw, leads_name, leads_origin = _pick_latest_session_df([
        ("c6_daily_leads_df", "Importacao diaria"),
        ("c6_operacao_leads_df", "Upload desta aba"),
    ])
    df_visao_raw, visao_name, visao_origin = _pick_latest_session_df([
        ("c6_daily_visao_df", "Importacao diaria"),
        ("c6_operacao_visao_df", "Upload desta aba"),
    ])

    if df_leads_raw is None:
        df_leads_raw, leads_name, leads_origin = _load_daily_import_cache("leads")
    if df_visao_raw is None:
        df_visao_raw, visao_name, visao_origin = _load_daily_import_cache("visao")
    else:
        df_leads_cache, leads_cache_name, leads_cache_origin = _load_daily_import_cache("leads")
        meta_daily = local_json_load(C6_DAILY_IMPORT_META, default={}) or {}
        cache_ts = _meta_cached_at((meta_daily or {}).get("leads") or {})
        session_ts = float(st.session_state.get("c6_daily_leads_df__ts", -1.0) or -1.0)
        if df_leads_cache is not None and cache_ts > session_ts:
            df_leads_raw, leads_name, leads_origin = df_leads_cache, leads_cache_name, leads_cache_origin
    if df_visao_raw is not None:
        df_visao_cache, visao_cache_name, visao_cache_origin = _load_daily_import_cache("visao")
        meta_daily = local_json_load(C6_DAILY_IMPORT_META, default={}) or {}
        cache_ts = _meta_cached_at((meta_daily or {}).get("visao") or {})
        session_ts = float(st.session_state.get("c6_daily_visao_df__ts", -1.0) or -1.0)
        if df_visao_cache is not None and cache_ts > session_ts:
            df_visao_raw, visao_name, visao_origin = df_visao_cache, visao_cache_name, visao_cache_origin
    df_ops_cache, ops_cache_name = _load_ops_import_cache()
    ops_meta = local_json_load(C6_OPS_CACHE_META, default={}) or {}
    ops_cache_ts = _meta_cached_at(ops_meta)
    ops_session_ts = float(st.session_state.get("c6_operacao_ops_df__ts", -1.0) or -1.0)
    if df_ops_raw is None or (df_ops_cache is not None and ops_cache_ts > ops_session_ts):
        df_ops_raw, ops_name = df_ops_cache, ops_cache_name

    if not (df_ops_raw is not None and df_leads_raw is not None and df_visao_raw is not None):
        faltantes = []
        if df_ops_raw is None:
            faltantes.append("XPrisma / Grelacd")
        if df_leads_raw is None:
            faltantes.append("Analitico Leads")
        if df_visao_raw is None:
            faltantes.append("Analitico Visao Cliente")
        st.info("Faltam arquivos para liberar a analise completa: " + ", ".join(faltantes) + ".")
        return
    signature = _c6_ops_signature(df_ops_raw, df_leads_raw, df_visao_raw, ops_name, leads_name, visao_name)
    last_signature = st.session_state.get("c6_operacao_last_signature")
    persist_now = bool(not view_only and signature != last_signature)
    try:
        result = _process_c6_operacao(df_ops_raw, df_leads_raw, df_visao_raw, persist_history=persist_now)
        if persist_now:
            _save_c6_remun_history(result, signature)
            st.session_state["c6_operacao_last_signature"] = signature
    except Exception as e:
        st.exception(e)
        return
    _render_c6_remun_config(result, view_only=view_only)
    _render_c6_remun_history()
    if operator_filter:
        result = _filter_c6_result_for_operator(result, operator_filter)
    st.session_state["c6_operacao_last_result"] = result
    st.session_state["c6_operacao_last_result__ts"] = dt.datetime.now().timestamp()
    resumo = result["resumo"]
    st.markdown("### Resumo executivo")
    top_cards = [
        ("Indicadores ACT", br_int(int((resumo.loc[resumo["Indicador"] == "Indicadores ACT", "Valor"].sum()))), None),
        ("Conversões até 14 dias", br_int(int((resumo.loc[resumo["Indicador"] == "Conversões 14 dias", "Valor"].sum()))), None),
        ("BKO 5+ dias úteis", br_int(int((resumo.loc[resumo["Indicador"] == "Clientes BKO 5+ dias úteis", "Valor"].sum()))), None),
        ("Qualificados válidos OMC", br_int(int((resumo.loc[resumo["Indicador"] == "Qualificados válidos OMC", "Valor"].sum()))), None),
    ]
    _render_metric_cards(top_cards)
    if not operator_filter:
        st.markdown("#### BKO 5+ dias úteis")
        st.caption("Acompanhamento operacional de clientes com permanência prolongada em BKO.")
        bko_status = result.get("bko_status_summary", pd.DataFrame())
        ativo_qtd = int(bko_status.loc[bko_status["status"].astype(str).eq("Ativo"), "quantidade"].sum()) if isinstance(bko_status, pd.DataFrame) and not bko_status.empty else 0
        inativo_qtd = int(bko_status.loc[bko_status["status"].astype(str).eq("Inativo"), "quantidade"].sum()) if isinstance(bko_status, pd.DataFrame) and not bko_status.empty else 0
        _render_metric_cards([
            ("Ativos na última base", br_int(ativo_qtd), None),
            ("Inativos pós BKO 5+", br_int(inativo_qtd), None),
        ])
        render_downloadable_table(_format_report_df(result["bko_alerta"]), "c6_bko_top", "bko_5mais_dias", raw_df=result["bko_alerta"])
    _, oper_tabs_map = _single_visible_tab(["ACT · Indicadores", "OCO · Abertura", "OQL · Qualificadores"], "c6_operacao_subtab", default="ACT · Indicadores")
    if "ACT · Indicadores" in oper_tabs_map:
      with oper_tabs_map["ACT · Indicadores"]:
        act_dia = result.get("act_diario_atual", pd.DataFrame())
        act_mensal = result.get("act_mensal", pd.DataFrame())
        dia_ref = ""
        if isinstance(act_dia, pd.DataFrame) and not act_dia.empty and "dia_comissao" in act_dia.columns:
            dia_ref = pd.to_datetime(act_dia["dia_comissao"], errors="coerce").max().strftime("%d/%m/%Y")
        mes_ref_atual = ""
        comissao_mes_atual = 0.0
        if isinstance(act_mensal, pd.DataFrame) and not act_mensal.empty and "mes_ref_comissao" in act_mensal.columns:
            mes_ref_atual = str(act_mensal["mes_ref_comissao"].dropna().astype(str).max() or "")
            comissao_mes_atual = float(pd.to_numeric(act_mensal.loc[act_mensal["mes_ref_comissao"].astype(str).eq(mes_ref_atual), "comissao_total"], errors="coerce").fillna(0.0).sum())
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Dia importado", dia_ref or "-")
        c2.metric("Abertas ACT no dia", br_int(int(act_dia["contas_abertas"].sum())) if isinstance(act_dia, pd.DataFrame) and not act_dia.empty else "0")
        c3.metric("Comissão ACT no dia", br_money(float(act_dia["comissao_total"].sum())) if isinstance(act_dia, pd.DataFrame) and not act_dia.empty else 0.0)
        c4.metric("Comissão ACT no mês", br_money(comissao_mes_atual), mes_ref_atual or None)
        st.markdown("#### Ranking diário ACT")
        if isinstance(act_dia, pd.DataFrame) and not act_dia.empty:
            render_downloadable_table(_format_report_df(act_dia), "c6_act_diario_atual", "c6_act_diario_atual", raw_df=act_dia)
        else:
            st.info("Nenhuma abertura ACT encontrada para o último dia importado.")
        st.markdown("#### Histórico mensal ACT")
        if isinstance(act_mensal, pd.DataFrame) and not act_mensal.empty:
            render_downloadable_table(_format_report_df(act_mensal), "c6_act_mensal", "c6_act_mensal", raw_df=act_mensal)
        else:
            st.info("Nenhum histórico mensal ACT encontrado.")
        st.markdown("#### Ranking acumulado ACT")
        render_downloadable_table(_format_report_df(result["act_operadores"]), "c6_act_oper", "c6_act_operadores", raw_df=result["act_operadores"])
        st.markdown("#### Conversão ACT por operador")
        act_conv = result.get("act_conversao_operadores", pd.DataFrame())
        if isinstance(act_conv, pd.DataFrame) and not act_conv.empty:
            render_downloadable_table(_format_report_df(act_conv), "c6_act_conversao_oper", "c6_act_conversao_operadores", raw_df=act_conv)
            if _downloads_enabled():
                st.download_button(
                    "Baixar PDF - Conversão ACT por operador",
                    data=_report_pdf_bytes("Conversão ACT por operador - C6 Empresas", dia_ref or mes_ref_atual, _operator_pdf_view("act_conversao", act_conv)),
                    file_name=f"conversao_act_operadores_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf",
                    mime="application/pdf",
                    use_container_width=True,
                )
        else:
            st.info("Nenhum dado de conversão ACT encontrado.")
        if not operator_filter:
            st.markdown("#### Qual perfil abre mais conta?")
            render_downloadable_table(_format_report_df(result["act_faixa"]), "c6_act_faixa", "c6_act_faixa", raw_df=result["act_faixa"])
        st.markdown("#### Analítico ACT")
        render_downloadable_table(_format_report_df(result["act_report"]), "c6_act_report", "c6_act_analitico", raw_df=result["act_report"])
    if "OCO · Abertura" in oper_tabs_map:
      with oper_tabs_map["OCO · Abertura"]:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Clientes trabalhados", br_int(int(result["oab_report"]["cnpj"].nunique())))
        c2.metric("Contas válidas p/ comissão", br_int(int(result["oab_report"]["abriu_apos_acao"].sum())))
        med = result["oab_report"].loc[result["oab_report"]["abriu_apos_acao"] == True, "dias_ate_abertura"].dropna()
        c3.metric("Tempo médio até abertura", f"{med.mean():.1f} dias".replace(".", ",") if len(med) else "0,0 dia")
        c4.metric("Comissão OCO", br_money(float(result["oab_report"]["valor_total"].sum())))
        st.markdown("#### Ranking de abertura OCO")
        render_downloadable_table(_format_report_df(result["oab_operadores"]), "c6_oco_oper", "c6_oco_operadores", raw_df=result["oab_operadores"])
        if not operator_filter:
            st.markdown("#### Aging BKO para sinalização ao banco")
            render_downloadable_table(_format_report_df(result["bko_summary"]), "c6_bko_summary", "c6_bko_summary", raw_df=result["bko_summary"])
        st.markdown("#### Analítico OCO")
        render_downloadable_table(_format_report_df(result["oab_report"]), "c6_oco_report", "c6_oco_analitico", raw_df=result["oab_report"])
    if "OQL · Qualificadores" in oper_tabs_map:
      with oper_tabs_map["OQL · Qualificadores"]:
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Clientes base", br_int(int(result["omc_report"]["cnpj"].nunique())))
        c2.metric("Qualificados válidos", br_int(int(result["omc_report"]["qualificado_valido"].sum())))
        c3.metric("Nível 4", br_int(int((result["omc_report"]["nivel_maximo"] >= 4).sum())))
        c4.metric("Valor teórico", br_money(float(result["omc_report"]["valor_teorico"].sum())))
        c5.metric("Valor real agora", br_money(float(result["omc_report"]["valor_real_agora"].sum())))
        st.markdown("#### Ranking de qualificadores OQL")
        render_downloadable_table(_format_report_df(result["omc_operadores"]), "c6_oql_oper", "c6_oql_operadores", raw_df=result["omc_operadores"])
        st.markdown("#### Qualificação por estágio M0 / M1 / M2")
        estagio = result["omc_report"][result["omc_report"]["qualificado_valido"]].groupby("estagio_m")["cnpj"].nunique().reset_index(name="clientes")
        if not estagio.empty:
            total_est = estagio["clientes"].sum()
            estagio["eficiencia_%"] = (estagio["clientes"] / total_est * 100).round(2)
        render_downloadable_table(_format_report_df(estagio), "c6_oql_estagio", "c6_oql_estagio", raw_df=estagio)
        st.markdown("#### Pix, Wallet e C6 Pay")
        pix_col = result["omc_report"].get("pix", pd.Series([""] * len(result["omc_report"])))
        if isinstance(pix_col, pd.DataFrame):
            pix_col = pix_col.iloc[:, 0]
        wallet_col = result["omc_report"].get("wallet", pd.Series([""] * len(result["omc_report"])))
        if isinstance(wallet_col, pd.DataFrame):
            wallet_col = wallet_col.iloc[:, 0]
        c6pay_col = result["omc_report"].get("c6pay_ativa_30", pd.Series([""] * len(result["omc_report"])))
        if isinstance(c6pay_col, pd.DataFrame):
            c6pay_col = c6pay_col.iloc[:, 0]
        pix_vals = ["" if pd.isna(v) else str(v).strip() for v in list(pix_col)]
        wallet_vals = ["" if pd.isna(v) else str(v).strip().upper() for v in list(wallet_col)]
        c6pay_vals = ["" if pd.isna(v) else str(v).strip().upper() for v in list(c6pay_col)]
        pix_com_qtd = sum(1 for v in pix_vals if v != "")
        pix_sem_qtd = sum(1 for v in pix_vals if v == "")
        pix_cnpj_qtd = sum(1 for v in pix_vals if _pix_has_cnpj(v))
        wallet_qtd = sum(1 for v in wallet_vals if v in ["1", "SIM", "TRUE", "S"])
        c6pay_qtd = sum(1 for v in c6pay_vals if v in ["1", "SIM", "TRUE", "S"])
        pix_sum = pd.DataFrame([
            {"Indicador": "Com Pix", "Quantidade": int(pix_com_qtd)},
            {"Indicador": "Sem Pix", "Quantidade": int(pix_sem_qtd)},
            {"Indicador": "Pix CNPJ", "Quantidade": int(pix_cnpj_qtd)},
            {"Indicador": "Com Wallet", "Quantidade": int(wallet_qtd)},
            {"Indicador": "C6 Pay ativa 30", "Quantidade": int(c6pay_qtd)},
        ])
        render_downloadable_table(_format_report_df(pix_sum), "c6_oql_pixsum", "c6_oql_pix_wallet_pay", raw_df=pix_sum)
        st.markdown("#### Analítico OQL")
        render_downloadable_table(_format_report_df(result["omc_report"]), "c6_oql_report", "c6_oql_analitico", raw_df=result["omc_report"])
    if _downloads_enabled():
        excel_bytes = _to_excel_bytes({"Resumo": result["resumo"], "ACT_Operadores": result["act_operadores"], "ACT_Conversao": result.get("act_conversao_operadores", pd.DataFrame()), "ACT_Analitico": result["act_report"], "OCO_Operadores": result["oab_operadores"], "OCO_BKO": result["bko_alerta"], "OCO_Analitico": result["oab_report"], "OQL_Operadores": result["omc_operadores"], "OQL_Analitico": result["omc_report"]})
        st.download_button("Baixar pacote completo da C6 Operação", data=excel_bytes, file_name=f"c6_operacao_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)
    else:
        st.caption("Pacote completo disponível ao ativar Preparar downloads.")

# =========================================================
# APP
# =========================================================
_page_icon_path = os.path.join(APP_DIR, "LOGO CORRETA.png")
st.set_page_config(
    page_title="Assis e Mollerke · C6",
    page_icon=_page_icon_path if os.path.exists(_page_icon_path) else None,
    layout="wide",
)
apply_theme()

if not login_gate():
    st.stop()

_bootstrap_cloud_from_bundled_data()
show_logo_and_title()
st.divider()
user_role = st.session_state.get("user_role", "admin")
operator_filter = st.session_state.get("operator_filter", "")
tab_labels = []
if user_role == "admin":
    tab_labels = ["Painel C6 Empresas", "Meta Supervisor C6", "Campanhas Meta", "Leads Diários", "Mensagens", "C6 Operação"]
elif user_role == "supervisor":
    tab_labels = ["Meta Supervisor C6", "C6 Operação"]
else:
    tab_labels = ["C6 Operação"]
_, tabs_map = _single_visible_tab(tab_labels, "main_tab_choice", default=tab_labels[0] if tab_labels else None)
if "_prepare_downloads" not in st.session_state:
    st.session_state["_prepare_downloads"] = False
st.toggle(
    "Preparar downloads",
    key="_prepare_downloads",
    help="Ative somente quando for baixar arquivos, PDFs ou montar anexos de e-mail.",
)

# =========================================================
# =====================  TAB 1  ===========================
# ===================== PAINEL C6 ==========================
# =========================================================
if "Painel C6 Empresas" in tabs_map:
  with tabs_map["Painel C6 Empresas"]:

    st.subheader("Importação diária")
    colA, colB = st.columns(2)
    with colA:
        up_c6 = st.file_uploader("Planilha C6 (Visão Cliente) — diária (.xlsx)", type=["xlsx"], accept_multiple_files=True, key="c6")
    with colB:
        up_leads = st.file_uploader("Planilha Leads — diária (.xlsx)", type=["xlsx"], accept_multiple_files=True, key="leads")

    invalid_c6_files = [f.name for f in (up_c6 or []) if not _filename_has_required_keyword(f.name, "visao")]
    invalid_leads_files = [f.name for f in (up_leads or []) if not _filename_has_required_keyword(f.name, "leads")]
    if invalid_c6_files:
        st.warning("Arquivo de Visão Cliente inválido. O nome do arquivo precisa conter pelo menos a palavra 'visão'.")
        st.caption("Arquivos recusados: " + ", ".join(invalid_c6_files))
        up_c6 = []
    if invalid_leads_files:
        st.warning("Arquivo de Leads inválido. O nome do arquivo precisa conter pelo menos a palavra 'leads'.")
        st.caption("Arquivos recusados: " + ", ".join(invalid_leads_files))
        up_leads = []

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

    _cmp_pending: Dict[str, dict] = {}

    if up_c6:
        for f in _sort_uploaded_c6_files(up_c6):
            raw_c6_bytes = f.getvalue()
            df_c6 = read_excel_any(raw_c6_bytes)
            df_c6_panel = _panel_c6_valid_df(df_c6)
            st.session_state["c6_daily_visao_df"] = df_c6.copy()
            st.session_state["c6_daily_visao_df__name"] = f.name
            st.session_state["c6_daily_visao_df__ts"] = dt.datetime.now().timestamp()
            _clear_c6_operacao_runtime_cache()
            if not _save_daily_import_cache("visao", f.name, raw_c6_bytes):
                st.error("Não consegui salvar a Visão Cliente na nuvem. A importação não ficará disponível em outros computadores.")

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

            mes_rel = detect_report_month_from_df(df_c6)
            _persist_visao_month_snapshot(df_c6)
            if mes_rel:
                try:
                    _refresh_current_month_remuneration_from_rows(fmt_month(mes_rel), _visao_month_rows_from_df(df_c6))
                except Exception as exc:
                    st.warning(f"Não foi possível recalcular a remuneração do mês importado agora: {exc}")
            _persist_visao_funil_track(df_c6)
            persist_supervisor_c6_daily(df_c6)

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

            if mes_rel and mes_rel >= HIST_START:
                mkey = fmt_month(mes_rel)

                df_tmp = df_c6_panel.copy()
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
                    "arquivo_c6": f.name if f else "",
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

            s_pix = df_c6.get(COL_PIX, pd.Series([""] * len(df_c6))).apply(_pix_clean_value)
            has_pix = s_pix.apply(_pix_is_valid)
            _cmp_pix_total = int(has_pix.sum())

            _cmp_cashin_total = float(df_c6[COL_CASHIN_MTD].sum())
            _cmp_base_receber_mes = float(_old_rule_receber_from_visao_df(df_c6, all_rows=True))

            if _cmp_day and _cmp_day >= HIST_START:
                day_key = fmt_date(_cmp_day)
                rec = _cmp_pending.get(day_key, {})
                rec.update({
                    "mes_ref": _cmp_mes_ref,
                    "c6_total": int(_cmp_c6_total or 0),
                    "qual_total": int(_cmp_qual_total or 0),
                    "qual_m0": int(_cmp_qual_m0 or 0),
                    "qual_m1": int(_cmp_qual_m1 or 0),
                    "qual_m2": int(_cmp_qual_m2 or 0),
                    "pix_total": int(_cmp_pix_total or 0),
                    "cashin_total": float(_cmp_cashin_total or 0.0),
                    "base_receber_mes": float(_cmp_base_receber_mes or 0.0),
                })
                _cmp_pending[day_key] = rec

    if up_leads:
        for f in _sort_uploaded_leads_files(up_leads):
            raw_leads_bytes = f.getvalue()
            df_leads = read_excel_any(raw_leads_bytes)
            st.session_state["c6_daily_leads_df"] = df_leads.copy()
            st.session_state["c6_daily_leads_df__name"] = f.name
            st.session_state["c6_daily_leads_df__ts"] = dt.datetime.now().timestamp()
            _clear_c6_operacao_runtime_cache()
            if not _save_daily_import_cache("leads", f.name, raw_leads_bytes):
                st.error("Não consegui salvar o arquivo de Leads na nuvem. A importação não ficará disponível em outros computadores.")
            _persist_leads_cnpj_track(df_leads)

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

            raw_leads_hash = file_md5(raw_leads_bytes)
            _upsert_leads_status_from_df(df_leads.copy(), source_name=f.name, source_hash=raw_leads_hash)
            st.session_state["c6_daily_leads_status_hash"] = raw_leads_hash

            _cmp_leads_total = int(len(df_leads))
            _cmp_day = detect_report_day_from_df(df_leads)
            if _cmp_day and _cmp_day >= HIST_START:
                day_key = fmt_date(_cmp_day)
                rec = _cmp_pending.get(day_key, {})
                if not rec.get("mes_ref"):
                    rec["mes_ref"] = fmt_month(dt.date(_cmp_day.year, _cmp_day.month, 1))
                rec["leads_total"] = int(_cmp_leads_total or 0)
                _cmp_pending[day_key] = rec

    st.divider()

    _daily_upload = bool((up_c6 and len(up_c6) > 0) or (up_leads and len(up_leads) > 0))
    _monthly_upload = bool(up_monthly and len(up_monthly) > 0)
    if (_daily_upload or _monthly_upload) and "firebase" not in st.secrets:
        _sync_items = []
        for _kind, _files in [("visao", up_c6 or []), ("leads", up_leads or []), ("mensal", up_monthly or [])]:
            for _f in _files:
                _sync_items.append([_kind, getattr(_f, "name", ""), getattr(_f, "size", 0)])
        _sync_sig = json.dumps(_sync_items, ensure_ascii=False, sort_keys=True)
        if _sync_sig and st.session_state.get("_last_local_cloud_sync_sig") != _sync_sig:
            with st.spinner("Sincronizando dados locais com o app online..."):
                _sync_ok, _sync_msg = _sync_local_data_to_cloud_seed("painel-c6-upload")
            st.session_state["_last_local_cloud_sync_sig"] = _sync_sig
            if _sync_ok:
                st.success("Dados publicados para o app online. O Streamlit pode levar alguns minutos para recarregar.")
            elif "Sem mudanças" not in _sync_msg:
                st.warning(_sync_msg)

    if _daily_upload and (df_c6 is None or df_c6.empty):
        df_c6_cache, _, _ = _load_daily_import_cache("visao")
        if df_c6_cache is not None and not df_c6_cache.empty:
            df_c6 = df_c6_cache.copy()
        else:
            df_c6_temp, df_c6_temp_name = _load_temp_import_daily_df("visao")
            if df_c6_temp is not None and not df_c6_temp.empty:
                df_c6 = df_c6_temp.copy()
                meta_tmp = local_json_load(C6_DAILY_IMPORT_META, default={}) or {}
                meta_tmp["visao"] = {
                    "name": str(df_c6_temp_name or meta_tmp.get("visao", {}).get("name", "")),
                    "cached_at": dt.datetime.now().isoformat(),
                }
                local_json_save(C6_DAILY_IMPORT_META, meta_tmp)

    if _daily_upload and (df_leads is None or df_leads.empty):
        df_leads_cache, _, _ = _load_daily_import_cache("leads")
        if df_leads_cache is not None and not df_leads_cache.empty:
            df_leads = df_leads_cache.copy()
        else:
            df_leads_temp, df_leads_temp_name = _load_temp_import_daily_df("leads")
            if df_leads_temp is not None and not df_leads_temp.empty:
                df_leads = df_leads_temp.copy()
                meta_tmp = local_json_load(C6_DAILY_IMPORT_META, default={}) or {}
                meta_tmp["leads"] = {
                    "name": str(df_leads_temp_name or meta_tmp.get("leads", {}).get("name", "")),
                    "cached_at": dt.datetime.now().isoformat(),
                }
                local_json_save(C6_DAILY_IMPORT_META, meta_tmp)

    _cache_visao_name = str((local_json_load(C6_DAILY_IMPORT_META, default={}) or {}).get("visao", {}).get("name", "") or "")
    _cache_leads_name = str((local_json_load(C6_DAILY_IMPORT_META, default={}) or {}).get("leads", {}).get("name", "") or "")
    _cache_visao_day = detect_report_day_from_df(df_c6) if df_c6 is not None and not df_c6.empty else None
    _cache_leads_day = detect_report_day_from_df(df_leads) if df_leads is not None and not df_leads.empty else None
    if _cache_visao_day and _cache_leads_day and _cache_visao_day != _cache_leads_day:
        st.warning(
            "Os últimos arquivos reaproveitados estão em datas diferentes. "
            f"Visão Cliente: {fmt_date(_cache_visao_day)} ({_cache_visao_name or 'sem nome'}) | "
            f"Leads: {fmt_date(_cache_leads_day)} ({_cache_leads_name or 'sem nome'}). "
            "Nessa situação, o comparativo diário não grava contas abertas zeradas por falta de Visão Cliente do mesmo dia."
        )

    if _daily_upload:
        _cmp_pending = _refresh_compare_pending_from_daily_c6(df_c6, _cmp_pending)
        _cmp_pending = _refresh_compare_pending_from_daily_leads(df_leads, _cmp_pending)

    saved_resumo = safe_json_load(HIST_RESUMO_MENSAL, default={})
    _panel_sig_now = _panel_c6_refresh_signature()
    _panel_refresh_meta = local_json_load(PANEL_C6_REFRESH_META, default={}) or {}
    _panel_sig_last = str(_panel_refresh_meta.get("signature", "") or "")
    _cached_incremental_df = _load_panel_c6_cached_df(PANEL_C6_INCREMENTAL_CACHE)
    _cached_cartilha_nova_df = _load_panel_c6_cached_df(PANEL_C6_CARTILHA_NOVA_CACHE)
    if not _cloud_fast_open() and not _daily_upload and not _monthly_upload:
        _remun_meta = safe_json_load(PANEL_C6_REFRESH_META, default={}) or {}
        _last_remun_key = str(_remun_meta.get("last_remun_refresh_key", "") or "")
        _import_meta_for_remun = local_json_load(C6_DAILY_IMPORT_META, default={}) or {}
        _visao_meta_for_remun = _import_meta_for_remun.get("visao", {}) or {}
        _remun_key_now = f"{REMUN_ENGINE_VERSION}|{str(_visao_meta_for_remun.get('cached_at') or _visao_meta_for_remun.get('name') or '')}"
        if _remun_key_now and _remun_key_now != _last_remun_key:
            df_c6_cache, _, _ = _load_daily_import_cache("visao")
            if df_c6_cache is not None and not df_c6_cache.empty:
                _mes_rel_remun = detect_report_month_from_df(df_c6_cache)
                if _mes_rel_remun:
                    _refresh_current_month_remuneration_from_rows(fmt_month(_mes_rel_remun), _visao_month_rows_from_df(df_c6_cache))
                    _cached_incremental_df = _load_panel_c6_cached_df(PANEL_C6_INCREMENTAL_CACHE)
                    _cached_cartilha_nova_df = _load_panel_c6_cached_df(PANEL_C6_CARTILHA_NOVA_CACHE)
                    _remun_meta["last_remun_refresh_key"] = _remun_key_now
                    safe_json_save(PANEL_C6_REFRESH_META, _remun_meta)
    _needs_full_panel_refresh = bool(_monthly_upload or ((_panel_sig_now != _panel_sig_last) and not _daily_upload and (_cached_incremental_df.empty or _cached_cartilha_nova_df.empty)))
    if _needs_full_panel_refresh:
        _panel_cartilha_nova_df = recompute_cartilha_nova()
        _panel_incremental_df = recompute_incremental()
        st.session_state["_panel_c6_incremental_df"] = _panel_incremental_df.copy()
        st.session_state["_panel_c6_cartilha_nova_df"] = _panel_cartilha_nova_df.copy()
        _save_panel_c6_cached_df(PANEL_C6_INCREMENTAL_CACHE, _panel_incremental_df)
        _save_panel_c6_cached_df(PANEL_C6_CARTILHA_NOVA_CACHE, _panel_cartilha_nova_df)
        if _monthly_upload:
            _refresh_panel_c6_histories_from_temp_imports(saved_resumo)
        else:
            _refresh_panel_c6_histories_from_current_daily(df_c6, df_leads, saved_resumo)
        local_json_save(PANEL_C6_REFRESH_META, {"signature": _panel_sig_now})
    elif _daily_upload or _panel_sig_now != _panel_sig_last:
        st.session_state["_panel_c6_incremental_df"] = _cached_incremental_df.copy()
        st.session_state["_panel_c6_cartilha_nova_df"] = _cached_cartilha_nova_df.copy()
        _refresh_panel_c6_histories_from_current_daily(df_c6, df_leads, saved_resumo)
        local_json_save(PANEL_C6_REFRESH_META, {"signature": _panel_sig_now})
    else:
        if "_panel_c6_incremental_df" not in st.session_state:
            st.session_state["_panel_c6_incremental_df"] = _cached_incremental_df
        if "_panel_c6_cartilha_nova_df" not in st.session_state:
            st.session_state["_panel_c6_cartilha_nova_df"] = _cached_cartilha_nova_df

    _compare_hist_existing = safe_json_load(HIST_COMPARE_DAILY, default={}) or {} if _cmp_pending else {}
    for day_key, rec in _cmp_pending.items():
        mes_ref = str(rec.get("mes_ref", "") or "")
        has_c6_metrics = any(k in rec for k in ["c6_total", "qual_total", "qual_m0", "qual_m1", "qual_m2", "pix_total", "cashin_total"])
        has_leads_metrics = "leads_total" in rec
        existing_row = _compare_hist_existing.get(day_key, {}) or {}

        if not has_c6_metrics and has_leads_metrics:
            if int(existing_row.get("c6_total", 0) or 0) == 0 and int(existing_row.get("qual_total", 0) or 0) == 0:
                _compare_hist_existing.pop(day_key, None)
                safe_json_save(HIST_COMPARE_DAILY, _compare_hist_existing)
            continue

        if "base_receber_mes" in rec:
            base_receber_mes = float(rec.get("base_receber_mes", 0.0) or 0.0)
        elif mes_ref and saved_resumo:
            base_receber_mes = float(saved_resumo.get(mes_ref, {}).get("receber_mes", 0.0))
        else:
            base_receber_mes = 0.0

        payload_cmp = {
            "mes_ref": mes_ref,
            "base_receber_mes": float(base_receber_mes),
        }
        if "c6_total" in rec:
            payload_cmp["c6_total"] = int(rec.get("c6_total", 0) or 0)
        if "leads_total" in rec:
            payload_cmp["leads_total"] = int(rec.get("leads_total", 0) or 0)
        if "qual_total" in rec:
            payload_cmp["qual_total"] = int(rec.get("qual_total", 0) or 0)
        if "qual_m0" in rec:
            payload_cmp["qual_m0"] = int(rec.get("qual_m0", 0) or 0)
        if "qual_m1" in rec:
            payload_cmp["qual_m1"] = int(rec.get("qual_m1", 0) or 0)
        if "qual_m2" in rec:
            payload_cmp["qual_m2"] = int(rec.get("qual_m2", 0) or 0)
        if "pix_total" in rec:
            payload_cmp["pix_total"] = int(rec.get("pix_total", 0) or 0)
        if "cashin_total" in rec:
            payload_cmp["cashin_total"] = float(rec.get("cashin_total", 0.0) or 0.0)
        compare_daily_upsert(day_key, payload_cmp)

    st.subheader("Comparativo diário (diferenças vs dia anterior)")

    df_cmp = compare_daily_df()
    if df_cmp.empty:
        st.info("Importe C6 e/ou Leads com DATA_BASE para começar o comparativo diário.")
    else:
        render_downloadable_table(df_cmp, "painel_cmp_diario", "painel_comparativo_diario", raw_df=df_cmp)
        raw_cmp = compare_daily_raw_df()
        dias_disp = sorted(raw_cmp["_date"].apply(lambda d: int(d.day)).unique().tolist()) if not raw_cmp.empty else []
        if dias_disp:
            st.markdown("**Comparar o mesmo dia entre os meses**")
            dia_padrao = int(raw_cmp["_date"].max().day)
            idx_padrao = dias_disp.index(dia_padrao) if dia_padrao in dias_disp else len(dias_disp) - 1
            dia_sel = st.selectbox(
                "Selecione o dia do mes para comparar entre os meses",
                dias_disp,
                index=idx_padrao,
                key="cmp_same_day_sel",
            )
            df_same_day = compare_same_day_across_months_df(int(dia_sel))
            if df_same_day.empty:
                st.info("Ainda nÃ£o hÃ¡ registros suficientes desse dia em meses diferentes.")
            else:
                render_downloadable_table(df_same_day, "painel_cmp_mesmo_dia", "painel_comparativo_mesmo_dia", raw_df=df_same_day)

    # =========================================================
    # ✅ AJUSTE (SÓ AQUI): regra ORIGINAL de mês (01/2026, 02/2026, 03/2026...)
    # + pintar a COLUNA % inteira (sem _pct aparecendo)
    # Regra: Abertas ÷ Cadastradas (no dia). Meta = 20%.
    # - Azul se >= 20%
    # - Vermelho se < 20%
    # =========================================================
    st.divider()
    st.subheader("Conversão diária (Leads indicadas × Contas abertas)")

    hist_open_day = hist_to_df(HIST_OPEN_DAILY, "Abertas")
    hist_leads_day = hist_to_df(HIST_LEADS_DAILY, "Cadastradas")

    if hist_open_day.empty and hist_leads_day.empty:
        st.info("Importe C6 e Leads (diário) para montar a conversão diária.")
    else:
        base_conv = pd.merge(hist_leads_day, hist_open_day, on="Data", how="outer").fillna(0)
        base_conv["Abertas"] = base_conv["Abertas"].astype(int)
        base_conv["Cadastradas"] = base_conv["Cadastradas"].astype(int)

        base_conv["% Conversão (dia)"] = base_conv.apply(
            lambda r: (r["Abertas"] / r["Cadastradas"]) if int(r["Cadastradas"]) > 0 else 0.0,
            axis=1
        )
        base_conv["Mes_ref"] = base_conv["Data"].map(month_first)

        meses_disp = sorted([m for m in base_conv["Mes_ref"].dropna().unique()])
        if not meses_disp:
            st.info("Ainda não há meses suficientes no histórico para conversão.")
        else:
            meses_lbl = [fmt_month(m) for m in meses_disp]
            mes_sel_lbl = st.selectbox(
                "Selecione o mês",
                meses_lbl,
                index=len(meses_lbl) - 1,
                key="conv_mes_sel"
            )
            mes_escolhido = meses_disp[meses_lbl.index(mes_sel_lbl)]
            mes_sel_key = fmt_month(mes_escolhido)

            mes_df = base_conv[base_conv["Mes_ref"] == mes_escolhido].copy()

            total_cad_mes = int(mes_df["Cadastradas"].sum())
            # Nesta visão de conversão, o usuário pediu para contar todas as contas
            # abertas do arquivo mensal consolidado, sem exclusões.
            total_ab_mes = int(_visao_month_openings_count(mes_sel_key))
            conv_mes = (total_ab_mes / total_cad_mes) if total_cad_mes > 0 else 0.0

            badge_mes = "am-badge-ok" if conv_mes >= ALVO_CONVERSAO else "am-badge-bad"
            st.markdown(
                f"<div class='{badge_mes}'>Conversão do mês selecionado ({mes_sel_key}): "
                f"{str(round(conv_mes*100,1)).replace('.',',')}%</div>",
                unsafe_allow_html=True
            )

            c1, c2, c3 = st.columns(3)
            c1.metric("Mês selecionado", mes_sel_key)
            c2.metric("Cadastradas (mês)", br_int(total_cad_mes))
            c3.metric("Abertas (mês)", br_int(total_ab_mes))

            view_conv = mes_df.sort_values("Data", ascending=False).reset_index(drop=True).copy()

            pct_series = view_conv["% Conversão (dia)"].astype(float).copy()

            display_df = pd.DataFrame({
                "Data": view_conv["Data"].apply(fmt_date),
                "Cadastradas": view_conv["Cadastradas"].apply(br_int),
                "Abertas": view_conv["Abertas"].apply(br_int),
                "% Conversão (dia)": pct_series.apply(lambda x: f"{str(round(float(x)*100,1)).replace('.',',')}%"),
            })

            def _style_pct_col(col: pd.Series):
                if col.name != "% Conversão (dia)":
                    return [""] * len(col)
                out = []
                for i in range(len(col)):
                    p = float(pct_series.iloc[i]) if i < len(pct_series) else 0.0
                    out.append("color:#007AFF;font-weight:900;" if p >= ALVO_CONVERSAO else "color:#FF3B30;font-weight:900;")
                return out

            styled = display_df.style.apply(_style_pct_col, axis=0)
            render_downloadable_table(styled, "painel_base_mes", "painel_base_mes", raw_df=display_df)

    # =========================================================
    # ✅ RELATÓRIOS (diário) — (igual ao antigo, com 4 abas)
    # - Usa o df_c6 importado do dia (Visão Cliente)
    # =========================================================
    st.subheader("Relatórios (diário)")

    if df_c6 is None or df_c6.empty:
        df_c6_cache, nome_c6_cache, origem_c6_cache = _load_daily_import_cache("visao")
        if df_c6_cache is not None and not df_c6_cache.empty:
            df_c6 = df_c6_cache.copy()
            st.caption(f"Usando o último diário importado: {origem_c6_cache} - {nome_c6_cache}")

    if df_c6 is None or df_c6.empty:
        st.info("Importe a planilha C6 (Visão Cliente) do dia para ver os relatórios diários.")
    else:
        _df = df_c6.copy()

        if COL_ABERTURA not in _df.columns:
            _df[COL_ABERTURA] = pd.NA
        if COL_FUNDACAO not in _df.columns:
            _df[COL_FUNDACAO] = pd.NA
        if COL_PIX not in _df.columns:
            _df[COL_PIX] = ""
        if COL_STATUS not in _df.columns:
            _df[COL_STATUS] = ""
        if COL_BR not in _df.columns:
            _df[COL_BR] = ""
        if COL_CASHIN_MTD not in _df.columns:
            _df[COL_CASHIN_MTD] = 0.0
        if COL_SALDO not in _df.columns:
            _df[COL_SALDO] = 0.0
        if COL_CRIT not in _df.columns:
            _df[COL_CRIT] = ""
        if COL_BY not in _df.columns:
            _df[COL_BY] = ""

        _df[COL_ABERTURA] = to_date_series(_df[COL_ABERTURA])
        _df[COL_FUNDACAO] = to_date_series(_df[COL_FUNDACAO])
        _df[COL_BR] = normalize_str(_df[COL_BR]).str.upper()
        _df[COL_STATUS] = normalize_str(_df[COL_STATUS])
        _df[COL_CASHIN_MTD] = pd.to_numeric(_df[COL_CASHIN_MTD], errors="coerce").fillna(0.0)
        _df[COL_SALDO] = pd.to_numeric(_df[COL_SALDO], errors="coerce").fillna(0.0)

        _rep_day = detect_report_day_from_df(_df)
        _rep_month = detect_report_month_from_df(_df)
        _rep_month_lbl = fmt_month(_rep_month) if _rep_month else ""

        if _rep_day:
            st.caption(f"Arquivo do dia: {fmt_date(_rep_day)} | Mês do relatório: {_rep_month_lbl}")
        else:
            st.caption(f"Mês do relatório: {_rep_month_lbl}")

        _, painel_rep_tabs = _single_visible_tab([
            "Aberturas",
            "Fundações (por dia)",
            "Pix + Status",
            "Qualificação + BR + Valores",
        ], "painel_rep_subtab", default="Aberturas")

        if "Aberturas" in painel_rep_tabs:
          with painel_rep_tabs["Aberturas"]:
            dfa = _df[_df[COL_ABERTURA].notna()].copy()
            if dfa.empty:
                st.info("Sem DT_CONTA_CRIADA no arquivo do dia.")
            else:
                dfa["_mes"] = pd.to_datetime(dfa[COL_ABERTURA], errors="coerce").dt.to_period("M").astype(str)
                meses = sorted([m for m in dfa["_mes"].dropna().unique()], reverse=False)

                if not meses:
                    st.info("Não consegui identificar mês pelas aberturas.")
                else:
                    meses_lbl = [f"{m.split('-')[1]}/{m.split('-')[0]}" for m in meses]
                    mes_sel_lbl = st.selectbox("Selecione o mês (Aberturas)", meses_lbl, index=len(meses_lbl) - 1, key="rep_ab_mes")
                    mes_sel = meses[meses_lbl.index(mes_sel_lbl)]

                    base = dfa[dfa["_mes"] == mes_sel].copy()

                    grp = (
                        base.groupby(COL_ABERTURA)
                        .size()
                        .reset_index(name="Aberturas")
                        .sort_values(COL_ABERTURA, ascending=False)
                    )
                    total_mes = int(grp["Aberturas"].sum())

                    c1, c2 = st.columns(2)
                    c1.metric("Total de aberturas (mês selecionado)", br_int(total_mes))
                    c2.metric("Dias com abertura", br_int(int(grp.shape[0])))

                    view = grp.copy()
                    view["Data"] = view[COL_ABERTURA].apply(fmt_date)
                    view["Aberturas"] = view["Aberturas"].apply(br_int)
                    view = view[["Data", "Aberturas"]]
                    render_downloadable_table(view, "painel_aberturas_dia", "painel_aberturas_dia", raw_df=view)

        if "Fundações (por dia)" in painel_rep_tabs:
          with painel_rep_tabs["Fundações (por dia)"]:
            dfa = _df[_df[COL_ABERTURA].notna()].copy()
            if dfa.empty:
                st.info("Sem DT_CONTA_CRIADA no arquivo do dia.")
            else:
                dias = sorted([d for d in dfa[COL_ABERTURA].dropna().unique()], reverse=True)
                dias_lbl = [fmt_date(d) for d in dias]

                if not dias:
                    st.info("Sem dias válidos de abertura.")
                else:
                    dia_sel_lbl = st.selectbox("Selecione o dia de abertura", dias_lbl, index=0, key="rep_fd_dia")
                    dia_sel = dias[dias_lbl.index(dia_sel_lbl)]

                    base = dfa[dfa[COL_ABERTURA] == dia_sel].copy()

                    fund = base[base[COL_FUNDACAO].notna()].copy()
                    if fund.empty:
                        st.info("Sem DT_FUNDACAO_EMPRESA preenchida para esse dia.")
                    else:
                        fund["_fund_my"] = fund[COL_FUNDACAO].apply(lambda x: f"{x.month:02d}/{x.year}" if isinstance(x, dt.date) else "")
                        fund["_fund_my"] = fund["_fund_my"].replace("", pd.NA)

                        tbl = (
                            fund.groupby("_fund_my")
                            .size()
                            .reset_index(name="Qtd")
                            .dropna(subset=["_fund_my"])
                        )

                        if tbl.empty:
                            st.info("Não consegui montar mês/ano de fundação.")
                        else:
                            def _my_key(s):
                                try:
                                    mm, yy = str(s).split("/")
                                    return int(yy) * 100 + int(mm)
                                except Exception:
                                    return 0

                            tbl = tbl.sort_values("_fund_my", key=lambda col: col.map(_my_key), ascending=False).reset_index(drop=True)

                            c1, c2 = st.columns(2)
                            c1.metric("Aberturas no dia", br_int(int(len(base))))
                            c2.metric("Com fundação preenchida", br_int(int(len(fund))))

                            view = tbl.rename(columns={"_fund_my": "Mês/Ano fundação", "Qtd": "Quantidade"}).copy()
                            view["Quantidade"] = view["Quantidade"].apply(br_int)
                            render_downloadable_table(view, "painel_fundacao_mes", "painel_fundacao_mes", raw_df=view)

        if "Pix + Status" in painel_rep_tabs:
          with painel_rep_tabs["Pix + Status"]:
            s = _df.get(COL_PIX, pd.Series([""] * len(_df))).apply(
                lambda x: _pix_clean_value(x) if _pix_is_valid(x) else ""
            )
            has_pix = s.apply(_pix_is_valid)

            pix_com = int(has_pix.sum())
            pix_sem = int((~has_pix).sum())

            stt = normalize_str(_df.get(COL_STATUS, pd.Series([""] * len(_df))))
            stt = stt.replace("", "SEM_STATUS")

            stt_tbl = (
                stt.value_counts()
                .rename_axis("Status")
                .reset_index(name="Quantidade")
            )

            domicilio_c6 = 0
            if COL_DOMICILIO in _df.columns:
                domicilio_c6 = int(_df[COL_DOMICILIO].apply(contains_c6).sum())

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Registros no arquivo", br_int(int(len(_df))))
            c2.metric("PIX (com)", br_int(pix_com))
            c3.metric("PIX (sem)", br_int(pix_sem))
            c4.metric("Domicílio C6", br_int(domicilio_c6))

            view = stt_tbl.copy()
            view["Quantidade"] = view["Quantidade"].apply(br_int)
            render_downloadable_table(view, "painel_pix_status", "painel_pix_status", raw_df=view)

        if "Qualificação + BR + Valores" in painel_rep_tabs:
          with painel_rep_tabs["Qualificação + BR + Valores"]:
            dqq = _panel_c6_valid_df(_df)
            dqq["_nivel"] = parse_level(dqq)
            dqq["_qual"] = dqq["_nivel"] >= 1

            qual_total = int(dqq["_qual"].sum())

            br_s = normalize_str(dqq.get(COL_BR, pd.Series([""] * len(dqq)))).str.upper()
            m0 = int((dqq["_qual"] & (br_s == "M0")).sum())
            m1 = int((dqq["_qual"] & (br_s == "M1")).sum())
            m2 = int((dqq["_qual"] & (br_s == "M2")).sum())

            cashin_total = float(dqq[COL_CASHIN_MTD].sum())
            saldo_total = float(dqq[COL_SALDO].sum())

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Qualificadas (arquivo)", br_int(qual_total))
            c2.metric("Qualificadas M0/M1/M2", f"{br_int(m0)} / {br_int(m1)} / {br_int(m2)}")
            c3.metric("Saldo total (VL_CASH_IN_MTD)", br_money(cashin_total))
            c4.metric("Saldo médio total (VL_SALDO_MEDIO_MENSALIZADO)", br_money(saldo_total))

            st.markdown("### Quebra por BR e Nível (qualificadas)")
            qbase = dqq[dqq["_qual"]].copy()
            if qbase.empty:
                st.info("Nenhuma qualificadas no arquivo (nível >= 1).")
            else:
                qbase["_br"] = normalize_str(qbase.get(COL_BR, pd.Series([""] * len(qbase)))).str.upper().replace("", "SEM_BR")
                qtbl = (
                    qbase.groupby(["_br", "_nivel"])
                    .size()
                    .reset_index(name="Qtd")
                    .sort_values(["_br", "_nivel"], ascending=[True, True])
                )
                view = qtbl.rename(columns={"_br": "BR", "_nivel": "Nível", "Qtd": "Quantidade"}).copy()
                view["Quantidade"] = view["Quantidade"].apply(br_int)
                render_downloadable_table(view, "painel_qualificados_dia", "painel_qualificados_dia", raw_df=view)

            st.markdown("### Base do mês (A receber no mês)")
            saved_resumo = safe_json_load(HIST_RESUMO_MENSAL, default={}) or {}
            base_receber = float(saved_resumo.get(_rep_month_lbl, {}).get("receber_mes", 0.0)) if _rep_month_lbl else 0.0
            st.metric("A receber (mês)", br_money(base_receber))

    st.divider()

    st.subheader("Resumo executivo (mês)")

    if _cloud_fast_open() and not _daily_upload:
        hist_open = pd.DataFrame()
        hist_leads = pd.DataFrame()
    else:
        hist_open = hist_to_df(HIST_OPEN_DAILY, "Abertas")
        hist_leads = hist_to_df(HIST_LEADS_DAILY, "Cadastradas")
    saved_resumo = safe_json_load(HIST_RESUMO_MENSAL, default={}) or {}

    if hist_open.empty and hist_leads.empty and not saved_resumo:
        st.info("Importe C6 + Leads (diário) para montar o mês.")
    else:
        if hist_open.empty and hist_leads.empty:
            base = pd.DataFrame(columns=["Data", "Abertas", "Cadastradas", "Mes_ref"])
        else:
            base = pd.merge(hist_leads, hist_open, on="Data", how="outer").fillna(0)
            base["Abertas"] = base["Abertas"].astype(int)
            base["Cadastradas"] = base["Cadastradas"].astype(int)
            base["Mes_ref"] = base["Data"].map(month_first)

        meses_daily = set(base["Mes_ref"].dropna().unique()) if "Mes_ref" in base.columns else set()
        meses_salvos = {dt.date(int(str(m).split("/")[1]), int(str(m).split("/")[0]), 1) for m in saved_resumo.keys() if "/" in str(m)}
        meses = sorted(meses_daily | meses_salvos)
        meses_lbl = [fmt_month(m) for m in meses]
        mes_lbl = st.selectbox("Selecione o mês do resumo executivo", meses_lbl, index=len(meses_lbl) - 1, key="painel_resumo_exec_mes")
        mes_atual = meses[meses_lbl.index(mes_lbl)]

        mes_df = base[base["Mes_ref"] == mes_atual].copy() if not base.empty else pd.DataFrame(columns=["Abertas", "Cadastradas"])
        # No resumo executivo do Painel C6, a abertura mensal também deve refletir
        # a soma bruta do histórico diário importado, sem exceções.
        total_ab_mes = int(mes_df["Abertas"].sum())
        total_cad_mes = int(mes_df["Cadastradas"].sum())
        perc_mes = (total_ab_mes / total_cad_mes) if total_cad_mes > 0 else 0.0

        badge = "am-badge-ok" if perc_mes >= ALVO_CONVERSAO else "am-badge-bad"
        st.markdown(
            f"<div class='{badge}'>Conversão do mês: {str(round(perc_mes*100,1)).replace('.',',')}%</div>",
            unsafe_allow_html=True
        )

        snap = {} if (_cloud_fast_open() and not _daily_upload) else safe_json_load(HIST_SNAPSHOT_MENSAL, default={})
        s = snap.get(mes_lbl, {})
        qual_mes = int((saved_resumo.get(mes_lbl) or {}).get("qualificadas", int(s.get("qualificadas_arquivo", 0))))

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Mês", mes_lbl)
        c2.metric("Cadastradas (mês)", br_int(total_cad_mes))
        c3.metric("Abertas (mês)", br_int(total_ab_mes))
        c4.metric("% geral (mês)", f"{str(round(perc_mes*100,1)).replace('.',',')}%")

        c5, c6, c7, c8 = st.columns(4)
        c5.metric("Saldo total (snapshot)", br_money(float(s.get("saldo_total", 0.0))))
        c6.metric("Pix (snapshot)", f'{br_int(int(s.get("pix_com",0)))} com | {br_int(int(s.get("pix_sem",0)))} sem')
        c7.metric("Domicílio C6 (snapshot)", br_int(int(s.get("domicilio_c6", 0))))
        c8.metric("Qualificadas (mês)", br_int(qual_mes))

    st.divider()

    st.subheader("Remuneração do mês atual (incremental)")

    if saved_resumo:
        months_sorted = sorted(saved_resumo.keys(), key=month_key_str)
        mes_atual = st.selectbox("Selecione o mês da remuneração", months_sorted, index=len(months_sorted) - 1, key="painel_remun_mes")
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
        st.info("Ainda não há histórico de remuneração. Importe os diários e/ou Nov/25 e Dez/25.")

    st.divider()

    st.subheader("Cartilha nova (abril a junho/26)")

    df_novo_calc = st.session_state.get("_panel_c6_cartilha_nova_df", pd.DataFrame())
    if not isinstance(df_novo_calc, pd.DataFrame):
        df_novo_calc = pd.DataFrame()
    if not df_novo_calc.empty:
        saved_novo = {}
        for _, row in df_novo_calc.iterrows():
            mkey = str(row.get("Mês", "") or "")
            if not mkey:
                continue
            saved_novo[mkey] = {
                "qualificadas": int(pd.to_numeric(pd.Series([row.get("Qualificadas", 0)]), errors="coerce").fillna(0).iloc[0]),
                "acelerador": float(pd.to_numeric(pd.Series([row.get("Acelerador", 1.0)]), errors="coerce").fillna(1.0).iloc[0]),
                "cash_in": int(pd.to_numeric(pd.Series([row.get("Cash In", 0)]), errors="coerce").fillna(0).iloc[0]),
                "spending": int(pd.to_numeric(pd.Series([row.get("Spending", 0)]), errors="coerce").fillna(0).iloc[0]),
                "c6pay": int(pd.to_numeric(pd.Series([row.get("C6 Pay", 0)]), errors="coerce").fillna(0).iloc[0]),
                "c6pay_credenciamento": int(pd.to_numeric(pd.Series([row.get("Credenciamento C6 Pay", 0)]), errors="coerce").fillna(0).iloc[0]),
                "pix_cnpj": int(pd.to_numeric(pd.Series([row.get("PIX CNPJ", 0)]), errors="coerce").fillna(0).iloc[0]),
                "wallet": int(pd.to_numeric(pd.Series([row.get("Wallet", 0)]), errors="coerce").fillna(0).iloc[0]),
                "deveria_receber": float(pd.to_numeric(pd.Series([row.get("Deveria receber (cheio)", 0.0)]), errors="coerce").fillna(0.0).iloc[0]),
                "ja_pago_ref": float(pd.to_numeric(pd.Series([row.get("Já pago (referência)", 0.0)]), errors="coerce").fillna(0.0).iloc[0]),
                "receber_mes": float(pd.to_numeric(pd.Series([row.get("A receber no mês", 0.0)]), errors="coerce").fillna(0.0).iloc[0]),
            }
    else:
        saved_novo = safe_json_load(HIST_NOVA_RESUMO_MENSAL, default={}) or {}
    if not saved_novo:
        st.info("Ainda não há base suficiente para a cartilha nova. Importe os arquivos diários de abril, maio e junho para comparar.")
    else:
        months_sorted_novo = sorted(saved_novo.keys(), key=month_key_str)
        mes_novo_sel = st.selectbox("Selecione o mês da cartilha nova", months_sorted_novo, index=len(months_sorted_novo) - 1)
        info_novo = saved_novo.get(mes_novo_sel, {})

        n1, n2, n3, n4 = st.columns(4)
        n1.metric("Mês", mes_novo_sel)
        n2.metric("Qualificadas", br_int(int(info_novo.get("qualificadas", 0))))
        n3.metric("Acelerador", f'{float(info_novo.get("acelerador", 1.0)):.2f}x')
        n4.metric("A receber (mês)", br_money(float(info_novo.get("receber_mes", 0.0))))

        n5, n6, n7, n8, n9, n10 = st.columns(6)
        n5.metric("Cash In", br_int(int(info_novo.get("cash_in", 0))))
        n6.metric("Spending", br_int(int(info_novo.get("spending", 0))))
        n7.metric("C6 Pay", br_int(int(info_novo.get("c6pay", 0))))
        n8.metric("Credenciamento C6 Pay", br_int(int(info_novo.get("c6pay_credenciamento", 0))))
        n9.metric("PIX CNPJ", br_int(int(info_novo.get("pix_cnpj", 0))))
        n10.metric("Wallet", br_int(int(info_novo.get("wallet", 0))))

    st.divider()

    st.subheader("Comparativo mensal de receita (regra antiga x cartilha nova)")

    saved_antigo = safe_json_load(HIST_RESUMO_MENSAL, default={}) or {}
    comp_rows = []
    comp_months = sorted(set(saved_antigo.keys()) | set(saved_novo.keys()), key=month_key_str)
    for mes in comp_months:
        antigo = float((saved_antigo.get(mes) or {}).get("receber_mes", 0.0))
        novo = float((saved_novo.get(mes) or {}).get("receber_mes", 0.0))
        maior = max(antigo, novo)
        regra = "Cartilha nova" if novo > antigo else "Regra antiga"
        if antigo == novo:
            regra = "Empate"
        comp_rows.append([mes, antigo, novo, maior, regra])

    if not comp_rows:
        st.info("Ainda não há dados para comparar a receita antiga com a cartilha nova.")
    else:
        df_comp = pd.DataFrame(comp_rows, columns=["Mês", "Regra antiga", "Cartilha nova", "Maior receita", "Regra vencedora"]).sort_values(
            "Mês", key=lambda col: col.map(month_key_str), ascending=True
        )
        view_comp = df_comp.copy()
        view_comp["Regra antiga"] = view_comp["Regra antiga"].apply(br_money)
        view_comp["Cartilha nova"] = view_comp["Cartilha nova"].apply(br_money)
        view_comp["Maior receita"] = view_comp["Maior receita"].apply(br_money)
        render_downloadable_table(view_comp, "painel_comp_receita", "painel_comparativo_receita", raw_df=df_comp)
        meses_analitico = df_comp["Mês"].astype(str).tolist()
        mes_analitico_sel = st.selectbox("Selecione o mês do analítico do cálculo", meses_analitico, index=len(meses_analitico) - 1)
        _cmp_cache_sig = st.session_state.get("_panel_c6_refresh_sig", "")
        _cmp_cache_key = f"{_cmp_cache_sig}|{mes_analitico_sel}"
        if _downloads_enabled():
            _cmp_cache_all = st.session_state.setdefault("_panel_comp_receita_cache", {})
            if _cmp_cache_key not in _cmp_cache_all:
                _cmp_cache_all[_cmp_cache_key] = _comparativo_receita_analytic_sheets(mes_analitico_sel)
            sheets_analitico = _cmp_cache_all[_cmp_cache_key]
            if any(not df.empty for df in sheets_analitico.values()):
                st.download_button(
                    "Baixar analítico antigo x novo",
                    data=_to_excel_bytes(sheets_analitico),
                    file_name=f"analitico_receita_antigo_novo_{mes_analitico_sel.replace('/', '-')}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key=f"dl_analitico_comp_receita_{mes_analitico_sel}",
                    use_container_width=False,
                )
        else:
            st.caption("Analítico para download disponível ao ativar Preparar downloads.")

        st.markdown("**Quadro comparativo antigo x novo (por critério / faixa)**")
        _quadro_cache_all = st.session_state.setdefault("_panel_quadro_comp_cache", {})
        if _cmp_cache_key not in _quadro_cache_all:
            _quadro_cache_all[_cmp_cache_key] = _old_new_comparative_quadro(mes_analitico_sel)
        quadro_pack = _quadro_cache_all[_cmp_cache_key]
        df_quadro = quadro_pack["quadro"]
        if df_quadro.empty:
            st.info("Sem base suficiente para montar o quadro comparativo detalhado desse mês.")
        else:
            view_quadro = df_quadro.copy()
            view_quadro["Clientes"] = view_quadro["Clientes"].apply(br_int)
            view_quadro["Faixa remuneração"] = view_quadro["Faixa remuneração"].apply(br_money)
            view_quadro["Valor cheio total"] = view_quadro["Valor cheio total"].apply(br_money)
            view_quadro["A receber total"] = view_quadro["A receber total"].apply(br_money)
            render_downloadable_table(view_quadro, "painel_quadro_antigo_novo", "painel_quadro_antigo_novo", raw_df=df_quadro)

        st.markdown("**Foco sugerido para o dia seguinte**")
        st.caption("Base analítica para priorização comercial por cliente.")
        _foco_vigente_cache = st.session_state.setdefault("_panel_foco_vigente_cache", {})
        if _cmp_cache_key not in _foco_vigente_cache:
            _foco_vigente_cache[_cmp_cache_key] = _focus_sugerido_base_vigente(mes_analitico_sel)
        foco_pack = _foco_vigente_cache[_cmp_cache_key]
        df_foco = foco_pack["foco"]
        if df_foco.empty:
            st.info("Não encontrei clientes com foco objetivo para o próximo dia útil na base vigente desse mês.")
        else:
            df_foco_resumo = foco_pack.get("resumo", pd.DataFrame())
            if isinstance(df_foco_resumo, pd.DataFrame) and not df_foco_resumo.empty:
                view_foco_resumo = df_foco_resumo.copy()
                view_foco_resumo["Clientes"] = view_foco_resumo["Clientes"].apply(br_int)
                view_foco_resumo["Receita_potencial"] = view_foco_resumo["Receita_potencial"].apply(br_money)
                render_downloadable_table(
                    view_foco_resumo,
                    "painel_foco_vigente_resumo",
                    "painel_foco_vigente_resumo",
                    raw_df=df_foco_resumo,
                )
            view_foco = df_foco.copy()
            for col in [
                "Receita adicional possível antiga",
                "Receita adicional possível nova",
                "Receita adicional prioritária",
                "Valor atual cartilha antiga",
                "Valor atual cartilha nova",
                "Já pago antigo",
                "Já pago novo",
                "A receber antigo hoje",
                "A receber novo hoje",
                "Meta foco principal",
                "Falta para meta",
                "Valor na próxima faixa",
                "Cash In atual",
                "Spending atual",
                "TPV C6 Pay atual",
            ]:
                if col in view_foco.columns:
                    view_foco[col] = view_foco[col].apply(br_money)
            for col in ["Data conta aberta", "Fundação empresa", "Mês abertura"]:
                if col in view_foco.columns:
                    view_foco[col] = view_foco[col].fillna("")
            render_downloadable_table(view_foco, "painel_foco_proximo_mes", "painel_foco_proximo_mes", raw_df=df_foco)

        if _downloads_enabled() and any(not df.empty for df in foco_pack["sheets"].values()):
            st.download_button(
                "Baixar quadro comparativo + foco",
                data=_to_excel_bytes(foco_pack["sheets"]),
                file_name=f"quadro_comparativo_foco_{mes_analitico_sel.replace('/', '-')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key=f"dl_quadro_comp_foco_{mes_analitico_sel}",
                use_container_width=False,
            )
        elif not _downloads_enabled():
            st.caption("Quadro comparativo para download disponível ao ativar Preparar downloads.")

    st.divider()

    st.subheader("Campanha 2º tri/26 (acompanhamento)")
    st.caption("Acompanhamento consolidado da campanha do 2º trimestre.")

    df_camp = compute_campanha_tri()
    if df_camp.empty:
        st.info("Ainda não há base suficiente para a campanha. Importe os arquivos diários de abril, maio e junho.")
    else:
        view_camp = df_camp.copy()
        for col in ["Aberturas", "Meta Aberturas", "Balde válido", "Qualificados", "Meta Qualificados", "Ativações C6 Pay", "Meta Ativações C6 Pay"]:
            if col in view_camp.columns:
                view_camp[col] = view_camp[col].apply(br_int)
        if "% Qualificação" in view_camp.columns:
            view_camp["% Qualificação"] = view_camp["% Qualificação"].apply(lambda x: f"{float(x)*100:.1f}%".replace(".", ","))
        if "% Mínimo" in view_camp.columns:
            view_camp["% Mínimo"] = view_camp["% Mínimo"].apply(lambda x: f"{float(x)*100:.1f}%".replace(".", ","))
        render_downloadable_table(view_camp, "painel_campanha_tri", "painel_campanha_tri", raw_df=df_camp)

    st.divider()

    with st.expander("Receita líquida (H1 + Assis e Mollerke)", expanded=False):

        saved = safe_json_load(HIST_RESUMO_MENSAL, default={})
        if not saved:
            st.info("Sem histórico mensal ainda. Importe os diários e/ou Nov/25 e Dez/25.")
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
            render_downloadable_table(df_liq, "painel_receita_liquida", "painel_receita_liquida", raw_df=df_liq)

    st.divider()

    st.subheader("Comparativo mensal de remuneração (regra antiga)")

    saved = safe_json_load(HIST_RESUMO_MENSAL, default={})
    if not saved:
        st.info("Sem histórico mensal ainda. Importe diários e/ou Nov/25 e Dez/25.")
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

        render_downloadable_table(view, "painel_remuneracao_antiga", "painel_remuneracao_antiga", raw_df=dfm)

        last = dfm.iloc[-1]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Último mês", str(last["Mês"]))
        c2.metric("Qualificadas", br_int(int(last["Qualificadas"])))
        c3.metric("Receita cheia", br_money(float(last["Deveria receber (cheio)"])))
        c4.metric("A receber", br_money(float(last["A receber no mês"])))

    st.divider()
    with st.expander("Opções avançadas", expanded=False):
        st.caption("Área restrita para manutenção do histórico do aplicativo.")
        if st.button("Resetar histórico do app", key="reset_hist_import_footer", use_container_width=False, type="secondary"):
            reset_all_data()
            st.success("Histórico resetado. Reimporte tudo novamente em lote.")




# =========================================================
# =====================  TAB 4  ===========================
# ================== 🏦 C6 OPERAÇÃO =======================
# =========================================================
if "C6 Operação" in tabs_map:
  with tabs_map["C6 Operação"]:
    _render_c6_operacao_tab(view_only=(user_role != "admin"), operator_filter=operator_filter if user_role == "operador" else "")

# =========================================================
# =====================  TAB 2  ===========================
# ================ 💬 Campanhas Meta =======================
# =========================================================
if "Meta Supervisor C6" in tabs_map:
  with tabs_map["Meta Supervisor C6"]:
    st.subheader("Meta Supervisor C6 Empresas")
    st.caption("Acompanhamento executivo da performance mensal do supervisor.")
    metas_store = _load_supervisor_c6_monthly_metas()
    hist_store = safe_json_load(HIST_SUPERVISOR_C6_DAILY, default={}) or {}
    month_options = set(metas_store.keys())
    month_options.update(_supervisor_month_from_day_key(k) for k in hist_store.keys())
    month_options.add(fmt_month(dt.date.today()))
    next_month = _shift_month_key(fmt_month(dt.date.today()), 1)
    if next_month:
        month_options.add(next_month)
    month_options = sorted([m for m in month_options if m and month_key_str(m) >= month_key_str("04/2026")], key=month_key_str)
    default_month = sorted([m for m in month_options if month_key_str(m) <= month_key_str(fmt_month(dt.date.today()))], key=month_key_str)[-1] if month_options else fmt_month(dt.date.today())
    selected_supervisor_month = st.selectbox(
        "Mês de competência da meta",
        month_options,
        index=month_options.index(default_month) if default_month in month_options else len(month_options) - 1,
        key="supervisor_c6_selected_month",
    )
    active_meta = _supervisor_c6_meta_for_month(selected_supervisor_month)
    st.caption(
        f"Meta de contas abertas ({selected_supervisor_month}): {br_int(int(active_meta['contas_abertas_meta']))} | "
        f"Prêmio: {br_money(float(active_meta['contas_abertas_premio']))}"
    )
    st.caption("Metas e prêmios são mensais e podem ser ajustados na própria tela.")

    with st.expander("Editar metas mensais do supervisor", expanded=False):
        edit_month = st.selectbox(
            "Mês para editar",
            month_options,
            index=month_options.index(selected_supervisor_month) if selected_supervisor_month in month_options else 0,
            key="supervisor_c6_edit_month",
        )
        meta_edit = _supervisor_c6_meta_for_month(edit_month)
        with st.form("supervisor_c6_monthly_meta_form"):
            st.markdown("**Metas por quantidade**")
            c1, c2, c3 = st.columns(3)
            contas_abertas_meta = c1.number_input("Contas abertas - meta", min_value=0, step=1, value=int(meta_edit["contas_abertas_meta"]))
            contas_abertas_premio = c2.number_input("Contas abertas - prêmio (R$)", min_value=0.0, step=10.0, value=float(meta_edit["contas_abertas_premio"]))
            instalacao_c6pay_meta = c3.number_input("Instalação C6 Pay - meta", min_value=0, step=1, value=int(meta_edit["instalacao_c6pay_meta"]))

            c4, c5, c6 = st.columns(3)
            instalacao_c6pay_premio = c4.number_input("Instalação C6 Pay - prêmio (R$)", min_value=0.0, step=10.0, value=float(meta_edit["instalacao_c6pay_premio"]))
            c6pay_ativada_meta = c5.number_input("C6 Pay ativada - meta", min_value=0, step=1, value=int(meta_edit["c6pay_ativada_meta"]))
            c6pay_ativada_premio = c6.number_input("C6 Pay ativada - prêmio (R$)", min_value=0.0, step=10.0, value=float(meta_edit["c6pay_ativada_premio"]))

            c7, c8, c9 = st.columns(3)
            domicilio_meta = c7.number_input("Domicílio qualificado - meta", min_value=0, step=1, value=int(meta_edit["domicilio_qualificado_meta"]))
            domicilio_premio = c8.number_input("Domicílio qualificado - prêmio (R$)", min_value=0.0, step=10.0, value=float(meta_edit["domicilio_qualificado_premio"]))
            spending_meta = c9.number_input("Spending qualificado - meta", min_value=0, step=1, value=int(meta_edit["spending_qualificado_meta"]))

            c10, c11, c12 = st.columns(3)
            spending_premio = c10.number_input("Spending qualificado - prêmio (R$)", min_value=0.0, step=10.0, value=float(meta_edit["spending_qualificado_premio"]))
            nivel4_meta = c11.number_input("Nível 4 - meta", min_value=0, step=1, value=int(meta_edit["nivel4_meta"]))
            nivel4_premio = c12.number_input("Nível 4 - prêmio (R$)", min_value=0.0, step=10.0, value=float(meta_edit["nivel4_premio"]))

            st.markdown("**Metas percentuais**")
            p1, p2, p3 = st.columns(3)
            pix_meta_pct = p1.number_input("Pix CNPJ - meta (%)", min_value=0.0, max_value=100.0, step=0.1, value=float(meta_edit["pix_cnpj_meta"]) * 100.0)
            pix_premio = p2.number_input("Pix CNPJ - prêmio (R$)", min_value=0.0, step=10.0, value=float(meta_edit["pix_cnpj_premio"]))
            wallet_meta_pct = p3.number_input("Wallet - meta (%)", min_value=0.0, max_value=100.0, step=0.1, value=float(meta_edit["wallet_meta"]) * 100.0)

            p4, p5, p6 = st.columns(3)
            wallet_premio = p4.number_input("Wallet - prêmio (R$)", min_value=0.0, step=10.0, value=float(meta_edit["wallet_premio"]))
            ativacao_cartao_meta_pct = p5.number_input("Ativação cartão - meta (%)", min_value=0.0, max_value=100.0, step=0.1, value=float(meta_edit["ativacao_cartao_meta"]) * 100.0)
            ativacao_cartao_premio = p6.number_input("Ativação cartão - prêmio (R$)", min_value=0.0, step=10.0, value=float(meta_edit["ativacao_cartao_premio"]))

            st.markdown("**Contas qualificadas - faixas**")
            faixas_base = sorted(meta_edit["contas_qualificadas_faixas"], key=lambda x: int(x["meta"]))
            while len(faixas_base) < 4:
                faixas_base.append({"meta": 0, "premio": 0.0})
            fcols = st.columns(4)
            qual_faixas = []
            for idx, faixa in enumerate(faixas_base[:4], start=1):
                with fcols[idx - 1]:
                    meta_f = st.number_input(f"Faixa {idx} - meta", min_value=0, step=1, value=int(faixa["meta"]), key=f"sup_qual_meta_{edit_month}_{idx}")
                    premio_f = st.number_input(f"Faixa {idx} - prêmio (R$)", min_value=0.0, step=10.0, value=float(faixa["premio"]), key=f"sup_qual_premio_{edit_month}_{idx}")
                    if meta_f > 0:
                        qual_faixas.append({"meta": int(meta_f), "premio": float(premio_f)})

            submitted = st.form_submit_button("Salvar metas do mês")
            if submitted:
                _save_supervisor_c6_monthly_meta(edit_month, {
                    "contas_abertas_meta": int(contas_abertas_meta),
                    "contas_abertas_premio": float(contas_abertas_premio),
                    "contas_qualificadas_faixas": qual_faixas,
                    "instalacao_c6pay_meta": int(instalacao_c6pay_meta),
                    "instalacao_c6pay_premio": float(instalacao_c6pay_premio),
                    "c6pay_ativada_meta": int(c6pay_ativada_meta),
                    "c6pay_ativada_premio": float(c6pay_ativada_premio),
                    "pix_cnpj_meta": float(pix_meta_pct) / 100.0,
                    "pix_cnpj_premio": float(pix_premio),
                    "domicilio_qualificado_meta": int(domicilio_meta),
                    "domicilio_qualificado_premio": float(domicilio_premio),
                    "spending_qualificado_meta": int(spending_meta),
                    "spending_qualificado_premio": float(spending_premio),
                    "wallet_meta": float(wallet_meta_pct) / 100.0,
                    "wallet_premio": float(wallet_premio),
                    "ativacao_cartao_meta": float(ativacao_cartao_meta_pct) / 100.0,
                    "ativacao_cartao_premio": float(ativacao_cartao_premio),
                    "nivel4_meta": int(nivel4_meta),
                    "nivel4_premio": float(nivel4_premio),
                })
                st.success(f"Metas de {edit_month} salvas. As tabelas serão recalculadas com a nova meta mensal.")
                st.rerun()

    df_supervisor, df_supervisor_mes, sup_summary = compute_supervisor_c6_meta(selected_supervisor_month)

    if df_supervisor.empty:
        st.info("Ainda nao ha base suficiente para a meta do supervisor. Importe o Visao Cliente de abril, maio e junho.")
    else:
        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Recebimento potencial", br_money(float(sup_summary.get("recebe_total", 0.0))))
        s2.metric("Metas batidas", f'{br_int(int(sup_summary.get("metas_batidas", 0)))} / {br_int(int(sup_summary.get("qtd_indicadores", 0)))}')
        s3.metric("Contas abertas", br_int(int(sup_summary.get("contas_abertas", 0))))
        s4.metric("Contas qualificadas", br_int(int(sup_summary.get("contas_qualificadas", 0))))

        s5, s6, s7 = st.columns(3)
        s5.metric("Pix CNPJ", f'{float(sup_summary.get("pix_pct", 0.0)) * 100:.1f}%'.replace(".", ","))
        s6.metric("Base Pix", br_int(int(sup_summary.get("pix_base", 0))))
        s7.metric("Pix CNPJ com chave", br_int(int(sup_summary.get("pix_cnpj", 0))))

        s8, s9, s10 = st.columns(3)
        s8.metric("Wallet", f'{float(sup_summary.get("wallet_pct", 0.0)) * 100:.1f}%'.replace(".", ","))
        s9.metric("Cartões entregues", br_int(int(sup_summary.get("cartoes_entregues", 0))))
        s10.metric("Com Wallet", br_int(int(sup_summary.get("wallets_cadastradas", 0))))

        st.divider()

        with st.expander("Consultar histórico mensal", expanded=False):
            st.caption("Selecione o mês para consultar o histórico consolidado.")
            monthly_history = sup_summary.get("monthly_history")
            if isinstance(monthly_history, pd.DataFrame) and not monthly_history.empty:
                hist_months = monthly_history["Mês"].astype(str).tolist()
                hist_default = selected_supervisor_month if selected_supervisor_month in hist_months else hist_months[-1]
                hist_sel = st.selectbox(
                    "Mês do histórico",
                    hist_months,
                    index=hist_months.index(hist_default),
                    key="supervisor_c6_history_month",
                )
                hist_row = monthly_history[monthly_history["Mês"].astype(str).eq(hist_sel)].iloc[0].to_dict()
                h1, h2, h3, h4 = st.columns(4)
                h1.metric("Mês", hist_sel)
                h2.metric("Última data-base", str(hist_row.get("Última data-base", "")))
                h3.metric("Recebimento potencial", br_money(float(hist_row.get("Recebimento potencial", 0.0) or 0.0)))
                h4.metric("Metas batidas", f"{br_int(int(hist_row.get('Metas batidas', 0) or 0))} / {br_int(int(hist_row.get('Indicadores', 0) or 0))}")
                h5, h6, h7 = st.columns(3)
                h5.metric("Contas abertas", br_int(int(hist_row.get("Contas abertas", 0) or 0)))
                h6.metric("Contas qualificadas", br_int(int(hist_row.get("Contas qualificadas", 0) or 0)))
                h7.metric("C6 Pay ativadas", br_int(int(hist_row.get("C6 Pay ativadas", 0) or 0)))
                h8, h9, h10 = st.columns(3)
                h8.metric("PIX CNPJ", f"{float(hist_row.get('PIX CNPJ %', 0.0) or 0.0) * 100:.1f}%".replace(".", ","))
                h9.metric("Wallet", f"{float(hist_row.get('Wallet %', 0.0) or 0.0) * 100:.1f}%".replace(".", ","))
                h10.metric("Ativação cartão", f"{float(hist_row.get('Ativação cartão %', 0.0) or 0.0) * 100:.1f}%".replace(".", ","))
            else:
                st.info("Ainda não há histórico mensal consolidado para o supervisor.")

        st.divider()

        view_supervisor = _format_supervisor_indicator_view(df_supervisor)
        render_downloadable_table(view_supervisor, "sup_indicadores", "supervisor_indicadores", raw_df=df_supervisor)
        st.caption("Contas qualificadas usam premiação progressiva conforme a faixa mensal.")

        st.divider()

        st.markdown("**Evolução diária**")
        view_supervisor_mes = df_supervisor_mes.copy()
        view_supervisor_mes = view_supervisor_mes.rename(columns={
            "Domicilio qualificado": "Domicílio qualificado",
            "Ativacao cartao %": "Ativação cartão %",
        })
        for col in [
            "Contas abertas", "Contas qualificadas", "C6 Pay ativadas",
            "Domicílio qualificado", "Spending qualificado"
        ]:
            if col in view_supervisor_mes.columns:
                view_supervisor_mes[col] = view_supervisor_mes[col].apply(br_int)
        for col in ["PIX CNPJ %", "Ativação cartão %"]:
            if col in view_supervisor_mes.columns:
                view_supervisor_mes[col] = view_supervisor_mes[col].apply(lambda x: x if isinstance(x, str) and str(x).strip().endswith("%") else f"{float(x) * 100:.1f}%".replace(".", ","))
        for col in ["Domicílio qualificado", "Spending qualificado"]:
            if col in view_supervisor_mes.columns:
                view_supervisor_mes[col] = view_supervisor_mes[col].apply(br_int)
        for col in ["PIX CNPJ %", "Ativação cartão %"]:
            if col in view_supervisor_mes.columns:
                view_supervisor_mes[col] = view_supervisor_mes[col].apply(lambda x: x if isinstance(x, str) and str(x).strip().endswith("%") else f"{float(x) * 100:.1f}%".replace(".", ","))
        def _fmt_percent_any(v):
            if pd.isna(v):
                return ""
            if isinstance(v, str):
                txt = v.strip()
                if not txt:
                    return ""
                if txt.endswith("%"):
                    return txt
                txt = txt.replace("%", "").replace(".", "").replace(",", ".")
                return f"{float(txt) * 100:.1f}%".replace(".", ",")
            return f"{float(v) * 100:.1f}%".replace(".", ",")

        view_supervisor_mes = view_supervisor_mes.rename(columns={
            "Domicilio qualificado": "Domicílio qualificado",
            "Ativacao cartao %": "Ativação cartão %",
            "DomicÃ­lio qualificado": "Domicílio qualificado",
            "AtivaÃ§Ã£o cartÃ£o %": "Ativação cartão %",
        })
        for col in ["Domicílio qualificado", "Spending qualificado"]:
            if col in view_supervisor_mes.columns:
                view_supervisor_mes[col] = view_supervisor_mes[col].apply(br_int)
        for col in ["PIX CNPJ %", "Ativação cartão %"]:
            if col in view_supervisor_mes.columns:
                view_supervisor_mes[col] = view_supervisor_mes[col].apply(_fmt_percent_any)
        if "Recebimento potencial" in view_supervisor_mes.columns:
            view_supervisor_mes["Recebimento potencial"] = view_supervisor_mes["Recebimento potencial"].apply(br_money)
        if "Delta potencial" in view_supervisor_mes.columns:
            view_supervisor_mes["Delta potencial"] = view_supervisor_mes["Delta potencial"].apply(lambda x: "" if pd.isna(x) else br_money(float(x)))

        render_downloadable_table(view_supervisor_mes, "sup_evolucao", "supervisor_evolucao_diaria", raw_df=df_supervisor_mes)
        st.caption("Wallet considera clientes válidos com Wallet marcado no Visão Cliente.")

        if _downloads_enabled():
            pdf_bytes = _supervisor_pdf_bytes(str(sup_summary.get("report_day", "")), sup_summary, view_supervisor, view_supervisor_mes)
            st.download_button(
                "Baixar relatório do supervisor (PDF)",
                data=pdf_bytes,
                file_name=f"meta_supervisor_c6_empresas_{str(sup_summary.get('report_day', '')).replace('/', '-') or 'atual'}.pdf",
                mime="application/pdf",
                use_container_width=False,
            )
        else:
            pdf_bytes = None
            st.caption("PDF disponível ao ativar Preparar downloads.")

        if user_role == "admin":
            st.divider()

            st.markdown("**Central de envio por e-mail**")
            if not _downloads_enabled():
                st.caption("Ative Preparar downloads para montar anexos e enviar e-mail.")
                st.stop()
            email_cfg = _load_supervisor_email_cfg()
            c6_last = _latest_c6_operacao_result_for_reports()
            report_day = str(sup_summary.get("report_day", ""))
            df_mail_leads, _, _ = _load_daily_import_cache("leads")
            df_mail_visao, _, _ = _load_daily_import_cache("visao")
            df_mail_lct, _, _ = _load_daily_import_cache("lct")
            leads_mail_base = _extract_leads_base(df_mail_leads) if df_mail_leads is not None else pd.DataFrame()
            visao_mail_base = _extract_visao_base(df_mail_visao) if df_mail_visao is not None else pd.DataFrame()
            leads_mail_hist = _load_funil_history_from_temp_imports("leads", _extract_leads_base)
            visao_mail_hist = _load_funil_history_from_temp_imports("visao", _extract_visao_base)
            if not leads_mail_base.empty:
                leads_mail_hist = pd.concat([leads_mail_hist, leads_mail_base], ignore_index=True) if not leads_mail_hist.empty else leads_mail_base
                leads_mail_hist = leads_mail_hist.sort_values([c for c in ["cnpj", "data_base", "data_hora_cadastro"] if c in leads_mail_hist.columns]).drop_duplicates("cnpj", keep="last")
            if not visao_mail_base.empty:
                visao_mail_hist = pd.concat([visao_mail_hist, visao_mail_base], ignore_index=True) if not visao_mail_hist.empty else visao_mail_base
                visao_mail_hist = visao_mail_hist.sort_values([c for c in ["cnpj", "data_base", "dt_conta_criada"] if c in visao_mail_hist.columns]).drop_duplicates("cnpj", keep="last")
            if leads_mail_hist.empty:
                leads_mail_hist = leads_mail_base
            if visao_mail_hist.empty:
                visao_mail_hist = visao_mail_base
            df_mail_lct_hist, _ = _load_lct_history_from_temp_imports(df_mail_lct)
            ura_resumo_mail, ura_analitico_mail = _build_ura_reports(df_mail_lct_hist, leads_mail_hist, visao_mail_hist)
            sbm_resumo_mail, sbm_analitico_mail = _build_sbm_reports(df_mail_lct_hist, leads_mail_hist, visao_mail_hist)
            follow_mail = _build_followup_daily_outputs(leads_mail_base, df_mail_visao) if not leads_mail_base.empty else {}

            report_options = []
            report_options.append({
                "key": "supervisor_pdf",
                "group": "Meta Supervisor C6",
                "label": "Supervisor C6 (PDF)",
                "filename": _supervisor_email_filename(report_day),
                "maintype": "application",
                "subtype": "pdf",
                "data": pdf_bytes,
            })
            report_options.append({
                "key": "supervisor_excel",
                "group": "Meta Supervisor C6",
                "label": "Supervisor C6 (Excel)",
                "filename": f"meta_supervisor_c6_empresas_{report_day.replace('/', '-') or 'atual'}.xlsx",
                "maintype": "application",
                "subtype": "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "data": _to_excel_bytes({"Supervisor": df_supervisor, "Evolucao_Diaria": df_supervisor_mes}),
            })
            if not ura_resumo_mail.empty:
                report_options.append({
                    "key": "ura_resumo_excel",
                    "group": "Leads Diários",
                    "label": "URA efetividade (Excel)",
                    "filename": f"ura_efetividade_{report_day.replace('/', '-') or 'atual'}.xlsx",
                    "maintype": "application",
                    "subtype": "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "data": _to_excel_bytes({"URA_Resumo": ura_resumo_mail}),
                })
            if not ura_analitico_mail.empty:
                report_options.append({
                    "key": "ura_analitico_excel",
                    "group": "Leads Diários",
                    "label": "URA analítico (Excel)",
                    "filename": f"ura_analitico_{report_day.replace('/', '-') or 'atual'}.xlsx",
                    "maintype": "application",
                    "subtype": "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "data": _to_excel_bytes({"URA_Analitico": ura_analitico_mail}),
                })
            if not sbm_resumo_mail.empty:
                report_options.append({
                    "key": "sbm_resumo_excel",
                    "group": "Leads Diários",
                    "label": "SBM Saber Mais efetividade (Excel)",
                    "filename": f"sbm_saber_mais_efetividade_{report_day.replace('/', '-') or 'atual'}.xlsx",
                    "maintype": "application",
                    "subtype": "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "data": _to_excel_bytes({"SBM_Resumo": sbm_resumo_mail}),
                })
            if not sbm_analitico_mail.empty:
                report_options.append({
                    "key": "sbm_analitico_excel",
                    "group": "Leads Diários",
                    "label": "SBM Saber Mais analítico (Excel)",
                    "filename": f"sbm_saber_mais_analitico_{report_day.replace('/', '-') or 'atual'}.xlsx",
                    "maintype": "application",
                    "subtype": "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "data": _to_excel_bytes({"SBM_Analitico": sbm_analitico_mail}),
                })
            if follow_mail and not follow_mail.get("mensagens", pd.DataFrame()).empty:
                data_envio_txt = dt.date.today().strftime("%d%m%Y")
                data_base_txt = str(follow_mail.get("data_base_txt") or dt.date.today().strftime("%d%m%Y"))
                msg_csv = follow_mail["mensagens"].to_csv(index=False, header=False, sep=";", lineterminator="\n").encode("utf-8-sig")
                report_options.append({
                    "key": "leads_followup_whatsapp_csv",
                    "group": "Leads Diários",
                    "label": "WhatsApp follow-up 1 a 15 dias (CSV)",
                    "filename": f"BOTXXXX_{data_base_txt}_{data_envio_txt}_INDICADOS_TOTAIS_1A15DIAS.csv",
                    "maintype": "text",
                    "subtype": "csv",
                    "data": msg_csv,
                })
            if follow_mail and not follow_mail.get("clientes_ura", pd.DataFrame()).empty:
                ura_csv = follow_mail["clientes_ura"].to_csv(index=False, header=True, sep=";", lineterminator="\n").encode("utf-8-sig")
                report_options.append({
                    "key": "leads_clientes_1a15_ura_csv",
                    "group": "Leads Diários",
                    "label": "Clientes 1 a 15 dias URA (CSV)",
                    "filename": "clientes um a 15 dias URa.csv",
                    "maintype": "text",
                    "subtype": "csv",
                    "data": ura_csv,
                })
            if c6_last:
                act_email_oper = _filter_act_email_rows(c6_last.get("act_operadores", pd.DataFrame()))
                act_email_report = _filter_act_email_rows(c6_last.get("act_report", pd.DataFrame()))
                act_email_conv = _filter_act_email_rows(c6_last.get("act_conversao_operadores", pd.DataFrame()))
                act_df = _operator_pdf_view("act", act_email_oper)
                act_conv_df = _operator_pdf_view("act_conversao", act_email_conv)
                oco_df = _operator_pdf_view("oco", c6_last.get("oab_operadores", pd.DataFrame()))
                oql_df = _operator_pdf_view("oql", c6_last.get("omc_operadores", pd.DataFrame()))
                report_options.extend([
                    {
                        "key": "act_pdf",
                        "group": "C6 Operação",
                        "label": "Operadores ACT (PDF)",
                        "filename": f"operadores_act_{report_day.replace('/', '-') or 'atual'}.pdf",
                        "maintype": "application",
                        "subtype": "pdf",
                        "data": _report_pdf_bytes("Operadores ACT - C6 Empresas", report_day, act_df),
                    },
                    {
                        "key": "act_excel",
                        "group": "C6 Operação",
                        "label": "Operadores ACT (Excel)",
                        "filename": f"operadores_act_{report_day.replace('/', '-') or 'atual'}.xlsx",
                        "maintype": "application",
                        "subtype": "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        "data": _to_excel_bytes({"ACT_Operadores": act_email_oper, "ACT_Conversao": act_email_conv, "ACT_Analitico": act_email_report}),
                    },
                    {
                        "key": "act_conversao_pdf",
                        "group": "C6 Operação",
                        "label": "Conversão ACT por operador (PDF)",
                        "filename": f"conversao_act_operadores_{report_day.replace('/', '-') or 'atual'}.pdf",
                        "maintype": "application",
                        "subtype": "pdf",
                        "data": _report_pdf_bytes("Conversão ACT por operador - C6 Empresas", report_day, act_conv_df),
                    },
                    {
                        "key": "act_conversao_excel",
                        "group": "C6 Operação",
                        "label": "Conversão ACT por operador (Excel)",
                        "filename": f"conversao_act_operadores_{report_day.replace('/', '-') or 'atual'}.xlsx",
                        "maintype": "application",
                        "subtype": "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        "data": _to_excel_bytes({"ACT_Conversao": act_email_conv}),
                    },
                    {
                        "key": "act_analitico_excel",
                        "group": "C6 Operação",
                        "label": "ACT analítico (Excel)",
                        "filename": f"act_analitico_{report_day.replace('/', '-') or 'atual'}.xlsx",
                        "maintype": "application",
                        "subtype": "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        "data": _to_excel_bytes({"ACT_Analitico": act_email_report}),
                    },
                    {
                        "key": "oco_pdf",
                        "group": "C6 Operação",
                        "label": "Operadores OCO (PDF)",
                        "filename": f"operadores_oco_{report_day.replace('/', '-') or 'atual'}.pdf",
                        "maintype": "application",
                        "subtype": "pdf",
                        "data": _report_pdf_bytes("Operadores OCO - C6 Empresas", report_day, oco_df),
                    },
                    {
                        "key": "oco_excel",
                        "group": "C6 Operação",
                        "label": "Operadores OCO (Excel)",
                        "filename": f"operadores_oco_{report_day.replace('/', '-') or 'atual'}.xlsx",
                        "maintype": "application",
                        "subtype": "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        "data": _to_excel_bytes({"OCO_Operadores": c6_last.get("oab_operadores", pd.DataFrame()), "OCO_Analitico": c6_last.get("oab_report", pd.DataFrame()), "BKO_5mais_dias": c6_last.get("bko_alerta", pd.DataFrame())}),
                    },
                    {
                        "key": "bko_analitico_excel",
                        "group": "C6 Operação",
                        "label": "BKO 5+ dias úteis (Excel)",
                        "filename": f"bko_5mais_dias_{report_day.replace('/', '-') or 'atual'}.xlsx",
                        "maintype": "application",
                        "subtype": "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        "data": _to_excel_bytes({"BKO_5mais_dias": c6_last.get("bko_alerta", pd.DataFrame())}),
                    },
                    {
                        "key": "oco_analitico_excel",
                        "group": "C6 Operação",
                        "label": "OCO analítico (Excel)",
                        "filename": f"oco_analitico_{report_day.replace('/', '-') or 'atual'}.xlsx",
                        "maintype": "application",
                        "subtype": "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        "data": _to_excel_bytes({"OCO_Analitico": c6_last.get("oab_report", pd.DataFrame())}),
                    },
                    {
                        "key": "oql_pdf",
                        "group": "C6 Operação",
                        "label": "Operadores OQL (PDF)",
                        "filename": f"operadores_oql_{report_day.replace('/', '-') or 'atual'}.pdf",
                        "maintype": "application",
                        "subtype": "pdf",
                        "data": _report_pdf_bytes("Operadores OQL - C6 Empresas", report_day, oql_df),
                    },
                    {
                        "key": "oql_excel",
                        "group": "C6 Operação",
                        "label": "Operadores OQL (Excel)",
                        "filename": f"operadores_oql_{report_day.replace('/', '-') or 'atual'}.xlsx",
                        "maintype": "application",
                        "subtype": "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        "data": _to_excel_bytes({"OQL_Operadores": c6_last.get("omc_operadores", pd.DataFrame()), "OQL_Analitico": c6_last.get("omc_report", pd.DataFrame())}),
                    },
                    {
                        "key": "oql_analitico_excel",
                        "group": "C6 Operação",
                        "label": "OQL analítico (Excel)",
                        "filename": f"oql_analitico_{report_day.replace('/', '-') or 'atual'}.xlsx",
                        "maintype": "application",
                        "subtype": "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        "data": _to_excel_bytes({"OQL_Analitico": c6_last.get("omc_report", pd.DataFrame())}),
                    },
                ])

            st.caption("Selecione os relatórios para envio. A lista usa a última importação salva no app.")
            selected_reports = []
            for group in ["Meta Supervisor C6", "Leads Diários", "C6 Operação"]:
                group_options = [opt for opt in report_options if opt.get("group") == group]
                if not group_options:
                    continue
                st.markdown(f"##### {group}")
                opt_cols = st.columns(2)
                for i, opt in enumerate(group_options):
                    with opt_cols[i % 2]:
                        csel, cdl = st.columns([4, 1])
                        opt_data = opt.get("data")
                        opt_available = isinstance(opt_data, (bytes, bytearray)) and len(opt_data) > 0
                        with csel:
                            checked = st.checkbox(
                                opt["label"],
                                value=(opt["key"] == "supervisor_pdf" and opt_available),
                                key=f"mail_opt_{opt['key']}",
                                disabled=not opt_available,
                            )
                            if not opt_available:
                                st.caption("Indisponível nesta importação.")
                        with cdl:
                            if opt_available:
                                st.download_button(
                                    "Baixar",
                                    data=bytes(opt_data),
                                    file_name=opt["filename"],
                                    mime=f"{opt['maintype']}/{opt['subtype']}",
                                    key=f"preview_mail_inline_{opt['key']}",
                                    use_container_width=True,
                                )
                        if checked and opt_available:
                            selected_reports.append(opt)

            selected_labels = [opt["label"] for opt in selected_reports]
            col_mail1, col_mail2 = st.columns([1.7, 1.1])
            with col_mail1:
                to_email = st.text_input(
                    "Destinatário",
                    value=email_cfg.get("to_email", SMTP_DEFAULT_TO),
                    key="supervisor_c6_to_email",
                    help="Você pode alterar este e-mail a qualquer momento. O app memoriza o último destinatário usado.",
                )
            with col_mail2:
                st.text_input("Remetente", value=SMTP_SENDER, disabled=True, key="supervisor_c6_from_email")

            subj_default = _email_subject_for_reports(report_day, selected_labels)
            body_default = _email_body_for_reports(report_day, selected_labels)
            st.session_state["supervisor_c6_subject"] = subj_default
            st.session_state["supervisor_c6_body"] = body_default
            subject = st.text_input("Assunto", value=subj_default, key="supervisor_c6_subject")
            body = st.text_area("Corpo do e-mail", value=body_default, height=160, key="supervisor_c6_body")

            smtp_password = st.text_input(
                "Senha do e-mail",
                value=email_cfg.get("smtp_password", SMTP_DEFAULT_PASSWORD),
                type="password",
                key="supervisor_c6_smtp_password",
                help="A senha fica salva localmente para facilitar os próximos envios.",
            )
            st.caption("Anexos selecionados")
            for opt in selected_reports:
                st.caption(f"- {opt['filename']}")
            st.caption(f"Cópia automática para {SMTP_SENDER}")

            if st.button("Enviar e-mail agora", key="send_supervisor_email_btn"):
                _save_supervisor_email_cfg(to_email, smtp_password)
                pwd = str(smtp_password or "").strip() or _smtp_password_from_secrets()
                attachments_to_send = [
                    opt for opt in selected_reports
                    if isinstance(opt.get("data"), (bytes, bytearray)) and len(opt.get("data")) > 0
                ]
                if not str(to_email or "").strip():
                    st.error("Preencha o e-mail de destino.")
                elif not pwd:
                    st.error("Preencha a senha do e-mail remetente para enviar.")
                elif not attachments_to_send:
                    st.error("Selecione ao menos um relatório para enviar.")
                else:
                    try:
                        send_email_with_attachments(
                            to_email=str(to_email).strip(),
                            smtp_password=pwd,
                            subject=str(subject or subj_default).strip(),
                            body=str(body or body_default).strip(),
                            attachments=attachments_to_send,
                        )
                        st.success("E-mail enviado com sucesso.")
                    except Exception as e:
                        st.error(f"Não consegui enviar o e-mail: {e}")

if "Campanhas Meta" in tabs_map:
  with tabs_map["Campanhas Meta"]:
    st.subheader("Campanhas Meta")

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

    def _read_meta_file(name: str, raw_bytes: bytes) -> Optional[pd.DataFrame]:
        try:
            name = name.lower()
            if name.endswith(".csv"):
                head = raw_bytes[:200_000]
                try:
                    sample = head.decode("utf-8-sig", errors="replace")
                except Exception:
                    sample = head.decode(errors="replace")
                sep = _detect_delimiter(sample)
                return pd.read_csv(
                    io.BytesIO(raw_bytes),
                    engine="python",
                    sep=sep,
                    on_bad_lines="skip",
                    encoding="utf-8-sig",
                )
            return pd.read_excel(io.BytesIO(raw_bytes))
        except Exception as e:
            st.error(f"Erro ao ler arquivo: {e}")
            return None

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
        s = safe_json_load(META_SUMMARY_PATH, default={}) or {}
        if not isinstance(s, dict):
            s = {}
        s["files"] = _normalize_files_meta_list(s.get("files", []))
        s["file_hashes"] = [f.get("hash") for f in s["files"] if isinstance(f, dict) and f.get("hash")]
        return s

    def _save_persisted_summary(summary: dict):
        if not isinstance(summary, dict):
            summary = {}
        summary["files"] = _normalize_files_meta_list(summary.get("files", []))
        summary["file_hashes"] = [f.get("hash") for f in summary["files"] if isinstance(f, dict) and f.get("hash")]
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

    def _update_summary_with_new_data(
        existing_summary: dict,
        new_df: pd.DataFrame,
        novos_metadados: List[dict]
    ) -> dict:
        if not isinstance(existing_summary, dict):
            existing_summary = {}
        existing_summary["files"] = _normalize_files_meta_list(existing_summary.get("files", []))

        _, old_daily = _normalize_existing_tables(existing_summary)

        df = new_df.copy()
        df["message_status"] = df["message_status"].astype(str).str.strip().str.lower()
        df["broadcast_description"] = df["broadcast_description"].astype(str)
        df["Data"] = df["message_date_time"].dt.date
        df["Mes"] = df["message_date_time"].dt.to_period("M").astype(str)

        status_set = set(existing_summary.get("status_set", []))
        campaign_set = set(existing_summary.get("campaign_set", []))
        status_set |= set(df["message_status"].dropna().unique().tolist())
        campaign_set |= set(df["broadcast_description"].dropna().unique().tolist())

        new_daily = df.groupby(["Mes", "Data", "message_status"]).size().reset_index(name="qty")

        if old_daily.empty:
            merged_daily = new_daily.copy()
        else:
            merged_daily = pd.concat([old_daily, new_daily], ignore_index=True)
            merged_daily["_k"] = (
                merged_daily["Mes"].astype(str) + "|" +
                merged_daily["Data"].astype(str) + "|" +
                merged_daily["message_status"].astype(str)
            )
            merged_daily = merged_daily.drop_duplicates(subset=["_k"], keep="last").drop(columns=["_k"])
            merged_daily = merged_daily.sort_values(["Mes", "Data", "message_status"]).reset_index(drop=True)

        merged_monthly = (
            merged_daily
            .groupby(["Mes", "message_status"], as_index=False)["qty"]
            .sum()
            .sort_values(["Mes", "message_status"])
            .reset_index(drop=True)
        )

        global_total = int(merged_monthly["qty"].sum()) if not merged_monthly.empty else 0
        global_enviados = int(
            merged_monthly[merged_monthly["message_status"].isin(["sent", "delivered", "read"])]["qty"].sum()
        ) if not merged_monthly.empty else 0
        dias_unicos = int(merged_daily["Data"].nunique()) if not merged_daily.empty else 0
        status_unicos = int(len(status_set))
        campanhas = int(len(campaign_set))

        files = _normalize_files_meta_list(existing_summary.get("files", []))
        for meta in (novos_metadados or []):
            if not isinstance(meta, dict):
                continue
            if not meta.get("name") or not meta.get("hash"):
                continue
            files = [f for f in files if f.get("name") != meta["name"]]
            files.append({"name": meta["name"], "hash": meta["hash"], "size": meta.get("size")})

        summary = {
            "updated_at": dt.datetime.now().isoformat(),
            "files": files,
            "file_hashes": [f.get("hash") for f in files if isinstance(f, dict) and f.get("hash")],
            "status_set": sorted(list(status_set)),
            "campaign_set": sorted(list(campaign_set)),
            "global": {
                "total": global_total,
                "enviados": global_enviados,
                "dias_unicos": dias_unicos,
                "campanhas": campanhas,
                "status_unicos": status_unicos,
            },
            "monthly": _records_firestore_safe(merged_monthly.to_dict(orient="records")),
            "daily": _records_firestore_safe(merged_daily.to_dict(orient="records")),
        }

        return summary

    def _groups_definitions() -> Dict[str, List[str]]:
        return {
            "VAREJO": ["BIGLOJ", "AMERIC", "VAREJO", "LINKS"],
            "FUNDACAO": ["FUNDACAO", "FFMEDI"],
            "EXPONENCIAL": ["EXPONENCIAL", "COPEL", "EMBASA", "BNB"],
            "I9": ["I9"],
            "JUNTA COMERCIAL": ["JACOM", "JUNTA"],
            "FIEB": ["FIEB", "CAIELL", "CSENAI", "CASESI", "CACIEB", "CIEB"],
        }

    def _match_group(broadcast_desc: str, group_name: str) -> bool:
        txt = (broadcast_desc or "").strip().upper()
        if not txt:
            return False
        keys = _groups_definitions().get(group_name, [])
        for k in keys:
            if k.upper() in txt:
                return True
        return False

    def _load_groups_store() -> dict:
        store = safe_json_load(META_GROUPS_PATH, default={}) or {}
        if not isinstance(store, dict):
            store = {}
        if "groups" not in store or not isinstance(store.get("groups"), dict):
            store["groups"] = {}
        store["file_hashes"] = [h for h in (store.get("file_hashes") or []) if isinstance(h, str) and h.strip()]
        store["files"] = _normalize_files_meta_list(store.get("files", []))
        return store

    def _save_groups_store(store: dict):
        if not isinstance(store, dict):
            store = {}
        if "groups" not in store or not isinstance(store.get("groups"), dict):
            store["groups"] = {}
        store["files"] = _normalize_files_meta_list(store.get("files", []))
        store["file_hashes"] = [f.get("hash") for f in store["files"] if isinstance(f, dict) and f.get("hash")]
        safe_json_save(META_GROUPS_PATH, store)

    def _compute_group_aggregates_from_raw_df(df5: pd.DataFrame) -> Dict[str, Dict[str, pd.DataFrame]]:
        out: Dict[str, Dict[str, pd.DataFrame]] = {}
        if df5.empty:
            return out

        df = df5.copy()
        df["message_status"] = df["message_status"].astype(str).str.strip().str.lower()
        df["broadcast_description"] = df["broadcast_description"].astype(str)
        df["Data"] = df["message_date_time"].dt.date
        df["Mes"] = df["message_date_time"].dt.to_period("M").astype(str)

        for gname in _groups_definitions().keys():
            mask = df["broadcast_description"].apply(lambda x: _match_group(x, gname))
            dfg = df[mask].copy()
            if dfg.empty:
                out[gname] = {
                    "monthly": pd.DataFrame(columns=["Mes", "message_status", "qty"]),
                    "daily": pd.DataFrame(columns=["Mes", "Data", "message_status", "qty"])
                }
                continue

            m = dfg.groupby(["Mes", "message_status"]).size().reset_index(name="qty")
            d = dfg.groupby(["Mes", "Data", "message_status"]).size().reset_index(name="qty")
            out[gname] = {"monthly": m, "daily": d}

        return out

    def _update_group_store_with_new_data(
        groups_store: dict,
        new_df: pd.DataFrame,
        novos_metadados: List[dict]
    ) -> dict:
        if not isinstance(groups_store, dict):
            groups_store = {}
        if "groups" not in groups_store or not isinstance(groups_store.get("groups"), dict):
            groups_store["groups"] = {}

        aggs = _compute_group_aggregates_from_raw_df(new_df)

        for gname in _groups_definitions().keys():
            parts = aggs.get(gname, {})
            new_daily = pd.DataFrame(parts.get("daily", []))
            if new_daily.empty:
                continue

            old = groups_store.get("groups", {}).get(gname, {})
            old_daily = pd.DataFrame(old.get("daily", []))

            if not old_daily.empty:
                old_daily["Mes"] = old_daily["Mes"].astype(str)
                old_daily["message_status"] = old_daily["message_status"].astype(str).str.lower()
                old_daily["qty"] = pd.to_numeric(old_daily["qty"], errors="coerce").fillna(0).astype(int)
                old_daily["Data"] = pd.to_datetime(old_daily["Data"], errors="coerce").dt.date

            new_daily = new_daily.copy()
            new_daily["Mes"] = new_daily["Mes"].astype(str)
            new_daily["message_status"] = new_daily["message_status"].astype(str).str.lower()
            new_daily["qty"] = pd.to_numeric(new_daily["qty"], errors="coerce").fillna(0).astype(int)
            new_daily["Data"] = pd.to_datetime(new_daily["Data"], errors="coerce").dt.date

            merged_daily = (
                pd.concat([old_daily, new_daily], ignore_index=True)
                if not old_daily.empty else new_daily.copy()
            )
            merged_daily["_k"] = (
                merged_daily["Mes"].astype(str) + "|" +
                merged_daily["Data"].astype(str) + "|" +
                merged_daily["message_status"].astype(str)
            )
            merged_daily = merged_daily.drop_duplicates(subset=["_k"], keep="last").drop(columns=["_k"])

            merged_monthly = merged_daily.groupby(["Mes", "message_status"], as_index=False)["qty"].sum()

            groups_store["groups"][gname] = {
                "daily": _records_firestore_safe(merged_daily.to_dict(orient="records")),
                "monthly": _records_firestore_safe(merged_monthly.to_dict(orient="records")),
                "updated_at": dt.datetime.now().isoformat(),
            }

        files = _normalize_files_meta_list(groups_store.get("files", []))
        for meta in (novos_metadados or []):
            if not isinstance(meta, dict):
                continue
            if not meta.get("name") or not meta.get("hash"):
                continue
            files = [f for f in files if f.get("name") != meta["name"]]
            files.append({"name": meta["name"], "hash": meta["hash"], "size": meta.get("size")})

        groups_store["files"] = files
        groups_store["file_hashes"] = [f.get("hash") for f in files if isinstance(f, dict) and f.get("hash")]
        groups_store["updated_at"] = dt.datetime.now().isoformat()

        return groups_store

    def _render_monthly_daily_tables(df_monthly: pd.DataFrame, df_daily: pd.DataFrame, key_prefix: str):
        if df_monthly.empty or df_daily.empty:
            st.info("Sem dados consolidados para este filtro.")
            return

        df_monthly = df_monthly.copy()
        df_daily = df_daily.copy()

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
        mes_sel_lbl = st.selectbox(
            "Selecione o mês",
            meses_lbl,
            index=len(meses_lbl) - 1,
            key=f"{key_prefix}__mes_sel"
        )
        mes_sel = meses[meses_lbl.index(mes_sel_lbl)]

        st.markdown("### Sintético mensal por status (mês selecionado)")
        mdf = df_monthly[df_monthly["Mes"] == mes_sel].copy().sort_values("qty", ascending=False)
        enviados_mes = int(mdf[mdf["message_status"].isin(["sent", "delivered", "read"])]["qty"].sum())

        a1, a2 = st.columns(2)
        a1.metric("Total no mês", _fmt_int_pt(int(mdf["qty"].sum())))
        a2.metric("Enviados no mês (sent+delivered+read)", _fmt_int_pt(enviados_mes))

        view_m = mdf.rename(columns={"message_status": "Status", "qty": "Quantidade"}).copy()
        view_m["Quantidade"] = view_m["Quantidade"].apply(_fmt_int_pt)
        render_downloadable_table(view_m, "meta_monthly", "campanhas_meta_mensal", raw_df=view_m)

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

            status_cols = [c for c in view_d.columns if c not in ["Data", "total_dia", "enviados_dia"]]
            col_order = ["Data"] + status_cols + ["total_dia", "enviados_dia"]
            view_d = view_d[[c for c in col_order if c in view_d.columns]]

            render_downloadable_table(view_d, "meta_daily", "campanhas_meta_diario", raw_df=view_d)

    def process_files_with_control_no_block(
        uploaded_files,
        control_path: str,
        process_func,
        tipo: str = "meta"
    ) -> Tuple[List[pd.DataFrame], List[dict], int, int, int]:
        control = get_file_control(control_path)
        files_meta = _normalize_files_meta_list(control.get("files", []))
        files_by_name = {
            f["name"]: f for f in files_meta
            if isinstance(f, dict) and f.get("name") and f.get("hash")
        }

        dfs: List[pd.DataFrame] = []
        novos_metadados: List[dict] = []

        qtd_novos = 0
        qtd_substituidos = 0
        qtd_reimportados_mesmo = 0

        for f in uploaded_files:
            raw = f.getvalue()
            h = file_md5(raw)

            if f.name in files_by_name:
                hash_anterior = files_by_name[f.name]["hash"]
                if hash_anterior == h:
                    qtd_reimportados_mesmo += 1
                else:
                    qtd_substituidos += 1
            else:
                qtd_novos += 1

            try:
                df = process_func(f.name, raw)
                if df is not None and not df.empty:
                    dfs.append(df)
                    novos_metadados.append({"name": f.name, "hash": h, "size": f.size})
            except Exception as e:
                st.error(f"Erro ao processar {f.name}: {e}")

        if novos_metadados:
            for meta in novos_metadados:
                files_meta = [x for x in files_meta if x.get("name") != meta["name"]]
                files_meta.append(meta)

            control["files"] = files_meta
            control["file_hashes"] = [x.get("hash") for x in files_meta if isinstance(x, dict) and x.get("hash")]
            control["updated_at"] = dt.datetime.now().isoformat()
            save_file_control(control_path, control)

        return dfs, novos_metadados, qtd_novos, qtd_substituidos, qtd_reimportados_mesmo

    if "meta_c6_summary" not in st.session_state:
        st.session_state["meta_c6_summary"] = _load_persisted_summary()

    with st.expander("Importar arquivos da Meta (CSV ou XLSX)", expanded=True):
        meta_files = st.file_uploader(
            "Envie um ou mais arquivos",
            type=["csv", "xlsx"],
            accept_multiple_files=True,
            key="meta_c6_upload"
        )

    if meta_files:
        dfs, novos_metadados, qtd_novos, qtd_substituidos, qtd_mesmo = process_files_with_control_no_block(
            meta_files,
            META_FILE_CONTROL,
            lambda name, raw: _read_meta_file(name, raw),
            "meta"
        )

        if qtd_substituidos > 0:
            st.warning(f"{qtd_substituidos} arquivo(s) foram reimportados com dados diferentes e substituíram a versão anterior.")
        if qtd_mesmo > 0:
            st.info(f"{qtd_mesmo} arquivo(s) reimportado(s) com o mesmo conteúdo foram reprocessados.")
        if qtd_novos > 0:
            st.info(f"{qtd_novos} novo(s) arquivo(s) adicionados.")

        if dfs:
            df_raw = pd.concat(dfs, ignore_index=True)
            df = _auto_rename_to_required(df_raw)

            required_cols = ["message_id", "message_date_time", "broadcast_description", "message_status", "contact_id"]
            missing = [c for c in required_cols if c not in df.columns]
            if missing:
                st.error(f"Colunas obrigatórias ausentes: {missing}")
            else:
                df = df[required_cols].copy()
                df["broadcast_description"] = df["broadcast_description"].astype(str)

                df = df[df["broadcast_description"].str.lower().str.contains("c6", na=False)]

                df["message_date_time"] = _parse_datetime_br_priority(df["message_date_time"])
                df = df.dropna(subset=["message_date_time"])

                if df.empty:
                    st.warning("Nenhum registro com 'c6' encontrado.")
                else:
                    existing = _load_persisted_summary()
                    novo_summary = _update_summary_with_new_data(existing, df, novos_metadados)
                    _save_persisted_summary(novo_summary)
                    st.session_state["meta_c6_summary"] = novo_summary

                    st.success(
                        f"Importação concluída: {qtd_novos} novo(s), {qtd_substituidos} substituído(s), {qtd_mesmo} reimportado(s)."
                    )
        else:
            st.warning("Nenhum arquivo gerou dados (todos vazios/ilegíveis).")

    summary = st.session_state.get("meta_c6_summary") or _load_persisted_summary()
    st.session_state["meta_c6_summary"] = summary

    if not summary:
        st.info("Importe um ou mais arquivos para gerar os relatórios.")
    else:
        g = summary.get("global", {})
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

        if not df_monthly.empty and not df_daily.empty:
            _render_monthly_daily_tables(df_monthly, df_daily, "meta_global")

        st.divider()
        with st.expander("Carteira — clique para abrir", expanded=False):
            st.caption("Baseado em broadcast_description. Regra: se a palavra aparecer em qualquer lugar do texto, contabiliza.")
            st.markdown("**Exemplos:** 'matheusameric' → VAREJO, 'i9hoje' → I9, 'fundacaox' → FUNDACAO")

            grp_files = st.file_uploader(
                "Enviar arquivos para preencher os grupos",
                type=["csv", "xlsx"],
                accept_multiple_files=True,
                key="meta_groups_upload"
            )

            grp_names = list(_groups_definitions().keys())
            grp_sel = st.selectbox("Grupo", grp_names, index=0, key="meta_groups_sel")

            if st.button("Processar", key="meta_groups_process_btn"):
                if not grp_files:
                    st.warning("Envie pelo menos 1 arquivo.")
                else:
                    dfs, novos_metadados, qtd_novos, qtd_substituidos, qtd_mesmo = process_files_with_control_no_block(
                        grp_files,
                        META_GROUPS_CONTROL,
                        lambda name, raw: _read_meta_file(name, raw),
                        "groups"
                    )

                    if qtd_substituidos > 0:
                        st.warning(f"{qtd_substituidos} arquivo(s) substituídos por versões mais recentes.")
                    if qtd_mesmo > 0:
                        st.info(f"{qtd_mesmo} arquivo(s) reimportado(s) com o mesmo conteúdo foram reprocessados.")

                    if dfs:
                        df_all = pd.concat(dfs, ignore_index=True)

                        df_all = _auto_rename_to_required(df_all)
                        required_cols = ["message_id", "message_date_time", "broadcast_description", "message_status", "contact_id"]
                        missing = [c for c in required_cols if c not in df_all.columns]

                        if not missing:
                            df_all = df_all[required_cols].copy()
                            df_all["broadcast_description"] = df_all["broadcast_description"].astype(str)
                            df_all["message_date_time"] = _parse_datetime_br_priority(df_all["message_date_time"])
                            df_all = df_all.dropna(subset=["message_date_time"])

                            if not df_all.empty:
                                groups_store = _load_groups_store()
                                groups_store = _update_group_store_with_new_data(groups_store, df_all, novos_metadados)
                                _save_groups_store(groups_store)

                                st.success(
                                    f"Grupos atualizados: {qtd_novos} novo(s), {qtd_substituidos} substituído(s), {qtd_mesmo} reimportado(s)."
                                )
                            else:
                                st.warning("Nenhum dado válido após processamento.")
                        else:
                            st.error(f"Colunas obrigatórias ausentes: {missing}")
                    else:
                        st.warning("Nenhum dado para processar (arquivos vazios/ilegíveis).")

            groups_store = _load_groups_store()
            gmap = groups_store.get("groups", {})
            grp_data = gmap.get(grp_sel, {})
            dfm_g = pd.DataFrame(grp_data.get("monthly", []))
            dfd_g = pd.DataFrame(grp_data.get("daily", []))

            st.markdown(f"## {grp_sel}")
            if dfm_g.empty or dfd_g.empty:
                st.info("Ainda não há dados nesse grupo. Envie arquivos e clique em **Processar**.")
            else:
                _render_monthly_daily_tables(dfm_g, dfd_g, f"meta_group__{grp_sel.replace(' ', '_').lower()}")


# =========================================================
# =====================  TAB 3  ===========================
# ================ 📋 LEADS DIÁRIOS (CORRIGIDO) ============
# =========================================================
if "Leads Diários" in tabs_map:
  with tabs_map["Leads Diários"]:

    st.subheader("Leads Diários")

    def _leads_status_load():
        return safe_json_load(LEADS_STATUS_DAILY_PATH, default={}) or {}

    def _leads_status_save(obj):
        safe_json_save(LEADS_STATUS_DAILY_PATH, obj)

    def _leads_status_reset_only():
        safe_json_delete(LEADS_STATUS_DAILY_PATH)
        safe_json_delete(LEADS_CONTROL_PATH)
        st.rerun()

    def _pick_latest_panel_df(kind: str):
        if kind == "leads":
            key = "c6_daily_leads_df"
        else:
            key = "c6_daily_visao_df"

        df_obj = st.session_state.get(key)
        name = str(st.session_state.get(f"{key}__name", "") or "")
        df_cache, cache_name, cache_origin = _load_daily_import_cache(kind)
        meta = local_json_load(C6_DAILY_IMPORT_META, default={}) or {}
        cache_ts = _meta_cached_at((meta or {}).get(kind) or {})
        session_ts = float(st.session_state.get(f"{key}__ts", -1.0) or -1.0)
        if df_obj is not None and (df_cache is None or session_ts >= cache_ts):
            return df_obj.copy(), name, "Painel C6 Empresas (sessão atual)"
        if df_cache is not None:
            return df_cache.copy(), cache_name, cache_origin
        return None, "", ""

    def _elapsed_days_inclusive(start_ts, end_ts) -> Optional[int]:
        if pd.isna(start_ts) or pd.isna(end_ts):
            return None
        try:
            start = pd.Timestamp(start_ts).normalize()
            end = pd.Timestamp(end_ts).normalize()
            diff = (end - start).days
            if diff < 0:
                return None
            return int(diff) + 1
        except Exception:
            return None

    def _bucket_days_1_15(value) -> str:
        if value is None or pd.isna(value):
            return ""
        try:
            n = int(value)
        except Exception:
            return ""
        if n <= 0:
            return ""
        if n <= 15:
            return str(n)
        return ">15"

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

    store = _leads_status_load() or {}

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

    with st.expander("Importação manual extraordinária", expanded=False):
        st.markdown("""
        **Regras (como você pediu):**
        * Data Base vem da **coluna B**.
        * Status vem da **coluna Q**.
        * **Sempre atualiza (UPSERT)** mesmo se for a mesma data.
        * Não ignora por hash/nome.
        * Indicações Válidas (≤14 dias): **DATA_BASE - DATA_HORA_CADASTRO <= 14**
        """)

        if "leads_upload_seq" not in st.session_state:
            st.session_state["leads_upload_seq"] = 0
        uploader_key = f"leads_status_upload_q_{st.session_state['leads_upload_seq']}"

        up_status_files = st.file_uploader(
            "Selecione os arquivos (XLSX/CSV). O histórico é ACUMULADO por dia (e o dia é sempre atualizado).",
            type=["xlsx", "csv"],
            accept_multiple_files=True,
            key=uploader_key
        )

        if False and up_status_files:
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

                s = df_status.iloc[:, 16].astype("string").fillna("").str.strip()
                s = s[s != ""]
                if s.empty:
                    status_counts = {}
                else:
                    s_limpo = s.apply(limpar_nome_status)
                    status_counts = s_limpo.value_counts().to_dict()
                    status_counts = {str(k): int(v) for k, v in status_counts.items()}

                validas = _calcular_validas_14d(df_status, data_base)

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

                last_processed_day = day_key_iso
                last_processed_at = imported_at

                processados += 1

            control["files"] = files_meta[-1000:]
            control["updated_at"] = dt.datetime.now().isoformat()

            if last_processed_day:
                control["last_processed"] = {
                    "day": last_processed_day,
                    "imported_at": last_processed_at,
                }

            safe_json_save(LEADS_CONTROL_PATH, control)
            _leads_status_save(store)

            st.success(f"{processados} arquivo(s) processado(s). Erros: {erros}.")

            st.session_state["leads_upload_seq"] += 1
            st.rerun()

    store = _leads_status_load() or {}
    control = safe_json_load(LEADS_CONTROL_PATH, default={}) or {}

    df_panel_leads, panel_leads_name, panel_leads_origin = _pick_latest_panel_df("leads")
    df_panel_visao, panel_visao_name, panel_visao_origin = _pick_latest_panel_df("visao")

    st.caption("Base diária consolidada a partir dos arquivos importados no Painel C6 Empresas.")
    info_cols = st.columns(2)
    with info_cols[0]:
        if panel_leads_name:
            st.caption(f"Leads em uso: {panel_leads_origin} - {panel_leads_name}")
        else:
            st.caption("Leads em uso: pendente")
    with info_cols[1]:
        if panel_visao_name:
            st.caption(f"Visão Cliente em uso: {panel_visao_origin} - {panel_visao_name}")
        else:
            st.caption("Visão Cliente em uso: pendente")

    with st.expander("Clientes da URA (Resumo LCT)", expanded=False):
        up_lct = st.file_uploader(
            "Importe o Resumo LCT do dia",
            type=["csv", "xlsx"],
            accept_multiple_files=False,
            key="leads_lct_upload",
        )
        if up_lct is not None:
            raw_lct_bytes = up_lct.getvalue()
            df_lct_tmp = _read_lct_file_any(up_lct.name, raw_lct_bytes)
            if df_lct_tmp is not None and not df_lct_tmp.empty:
                st.session_state["c6_daily_lct_df"] = _compact_lct_cache_df(df_lct_tmp)
                st.session_state["c6_daily_lct_df__name"] = up_lct.name
                if _save_daily_import_cache("lct", up_lct.name, raw_lct_bytes):
                    st.success("Resumo LCT importado.")
                else:
                    st.error("Não consegui salvar o Resumo LCT na nuvem. A importação não ficará disponível em outros computadores.")
                if "firebase" not in st.secrets:
                    _lct_sync_sig = json.dumps(["lct", up_lct.name, getattr(up_lct, "size", 0)], ensure_ascii=False)
                    if st.session_state.get("_last_lct_cloud_sync_sig") != _lct_sync_sig:
                        with st.spinner("Sincronizando Resumo LCT com o app online..."):
                            _sync_ok, _sync_msg = _sync_local_data_to_cloud_seed("leads-lct-upload")
                        st.session_state["_last_lct_cloud_sync_sig"] = _lct_sync_sig
                        if _sync_ok:
                            st.success("Resumo LCT publicado para o app online. O Streamlit pode levar alguns minutos para recarregar.")
                        elif "Sem mudanças" not in _sync_msg:
                            st.warning(_sync_msg)

        df_panel_lct, panel_lct_name, panel_lct_origin = _load_daily_import_cache("lct")
        df_ura_lct, ura_lct_names = _load_lct_history_from_temp_imports(df_panel_lct)
        if df_panel_lct is not None and not df_panel_lct.empty:
            df_ura_lct = pd.concat([df_ura_lct, df_panel_lct], ignore_index=True, sort=False) if df_ura_lct is not None and not df_ura_lct.empty else df_panel_lct
        if ura_lct_names:
            st.caption(f"Histórico Resumo LCT em uso: {ura_lct_names}")
        elif panel_lct_name:
            st.caption(f"Resumo LCT em uso: {panel_lct_origin} - {panel_lct_name}")
        else:
            st.caption("Resumo LCT em uso: pendente")

    if df_panel_leads is None or df_panel_visao is None:
        faltantes = []
        if df_panel_leads is None:
            faltantes.append("Leads")
        if df_panel_visao is None:
            faltantes.append("Visão Cliente")
        st.info(f"Para liberar toda a leitura desta aba, importe no Painel C6 Empresas: {', '.join(faltantes)}.")

    if df_panel_leads is not None:
        leads_funil = _extract_leads_base(df_panel_leads)
        visao_funil = _extract_visao_base(df_panel_visao) if df_panel_visao is not None else pd.DataFrame()

        if not visao_funil.empty:
            merge_cols = [c for c in ["cnpj", "dt_conta_criada", "nome_cliente"] if c in visao_funil.columns]
            leads_funil = leads_funil.merge(
                visao_funil[merge_cols].rename(columns={"dt_conta_criada": "dt_conta_criada_visao", "nome_cliente": "nome_cliente_visao"}),
                on="cnpj",
                how="left"
            )
            leads_funil["nome_cliente"] = leads_funil["nome_cliente"].fillna("").replace("", pd.NA).fillna(leads_funil["nome_cliente_visao"])
        else:
            leads_funil["dt_conta_criada_visao"] = pd.NaT

        leads_funil["dt_abertura_ref"] = leads_funil["dt_conta_aberta_leads"]
        leads_funil.loc[leads_funil["dt_abertura_ref"].isna(), "dt_abertura_ref"] = leads_funil.loc[
            leads_funil["dt_abertura_ref"].isna(), "dt_conta_criada_visao"
        ]
        leads_funil["dias_para_abrir"] = leads_funil.apply(
            lambda r: _elapsed_days_inclusive(r.get("data_hora_cadastro"), r.get("dt_abertura_ref")), axis=1
        )
        leads_funil["abriu_conta"] = leads_funil["dt_abertura_ref"].notna()
        leads_funil["faixa_abertura_15d"] = leads_funil["dias_para_abrir"].apply(_bucket_days_1_15)
        leads_funil["dias_corridos_base"] = leads_funil.apply(
            lambda r: _elapsed_days_inclusive(r.get("data_hora_cadastro"), r.get("data_base"))
            if pd.notna(r.get("data_base")) else None,
            axis=1
        )
        leads_funil["faixa_pipeline_15d"] = leads_funil["dias_corridos_base"].apply(_bucket_days_1_15)

        st.divider()
        st.markdown("### Tempo até abertura da conta")

        opened_leads = leads_funil[leads_funil["abriu_conta"] & leads_funil["dias_para_abrir"].notna()].copy()
        avg_open_days = float(opened_leads["dias_para_abrir"].mean()) if not opened_leads.empty else 0.0
        med_open_days = float(opened_leads["dias_para_abrir"].median()) if not opened_leads.empty else 0.0
        within_15 = int(opened_leads["dias_para_abrir"].fillna(9999).le(15).sum()) if not opened_leads.empty else 0
        report_month_visao = detect_report_month_from_df(df_panel_visao) if df_panel_visao is not None and not df_panel_visao.empty else None
        total_opened = _visao_df_openings_count(df_panel_visao, report_month_visao)

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Leads na base", br_int(int(leads_funil["cnpj"].nunique())))
        m2.metric("Contas abertas", br_int(total_opened))
        m3.metric("Tempo médio para abrir", f"{avg_open_days:.1f} dias".replace(".", ","))
        m4.metric("Mediana", f"{med_open_days:.1f} dias".replace(".", ","))

        if not opened_leads.empty:
            dist_rows = []
            total_opened_rows = len(opened_leads)
            for day_num in range(1, 16):
                qtd = int(opened_leads["dias_para_abrir"].eq(day_num).sum())
                pct = (qtd / total_opened_rows * 100.0) if total_opened_rows > 0 else 0.0
                dist_rows.append({
                    "Dia após cadastro": f"D{day_num}",
                    "Clientes": qtd,
                    "% das aberturas": pct,
                })
            qtd_gt15 = int(opened_leads["dias_para_abrir"].gt(15).sum())
            pct_gt15 = (qtd_gt15 / total_opened_rows * 100.0) if total_opened_rows > 0 else 0.0
            dist_rows.append({
                "Dia após cadastro": ">15",
                "Clientes": qtd_gt15,
                "% das aberturas": pct_gt15,
            })
            df_dist = pd.DataFrame(dist_rows)
            df_dist["Clientes"] = df_dist["Clientes"].apply(br_int)
            df_dist["% das aberturas"] = df_dist["% das aberturas"].apply(lambda x: f"{x:.1f}%".replace(".", ","))
            render_downloadable_table(df_dist, "leads_prazo_abertura", "leads_prazo_abertura", raw_df=df_dist)

        pending_pipeline = leads_funil[~leads_funil["abriu_conta"] & leads_funil["data_base"].notna()].copy()
        if not pending_pipeline.empty:
            pipe_rows = []
            total_pending = len(pending_pipeline)
            for day_num in range(1, 16):
                qtd = int(pending_pipeline["dias_corridos_base"].eq(day_num).sum())
                pct = (qtd / total_pending * 100.0) if total_pending > 0 else 0.0
                pipe_rows.append({
                    "Aging atual do lead": f"D{day_num}",
                    "Clientes pendentes": qtd,
                    "% da carteira pendente": pct,
                })
            qtd_gt15 = int(pending_pipeline["dias_corridos_base"].gt(15).sum())
            pct_gt15 = (qtd_gt15 / total_pending * 100.0) if total_pending > 0 else 0.0
            pipe_rows.append({
                "Aging atual do lead": ">15",
                "Clientes pendentes": qtd_gt15,
                "% da carteira pendente": pct_gt15,
            })
            st.markdown("#### 🔎 Onde o lead ainda está parado hoje")
            df_pipe = pd.DataFrame(pipe_rows)
            df_pipe["Clientes pendentes"] = df_pipe["Clientes pendentes"].apply(br_int)
            df_pipe["% da carteira pendente"] = df_pipe["% da carteira pendente"].apply(lambda x: f"{x:.1f}%".replace(".", ","))
            render_downloadable_table(df_pipe, "leads_carteira_pendente", "leads_carteira_pendente", raw_df=df_pipe)

        detalhe_cols = [
            c for c in [
                "nome_cliente", "cnpj", "data_base", "data_hora_cadastro", "dt_abertura_ref",
                "dias_para_abrir", "status_abertura_conta", "status_final", "pendencias", "dias_corridos_base"
            ] if c in leads_funil.columns
        ]
        detalhe_leads = leads_funil[detalhe_cols].copy()
        if "data_base" in detalhe_leads.columns:
            detalhe_leads["data_base"] = pd.to_datetime(detalhe_leads["data_base"], errors="coerce").dt.strftime("%d/%m/%Y")
        if "data_hora_cadastro" in detalhe_leads.columns:
            detalhe_leads["data_hora_cadastro"] = pd.to_datetime(detalhe_leads["data_hora_cadastro"], errors="coerce").dt.strftime("%d/%m/%Y %H:%M")
        if "dt_abertura_ref" in detalhe_leads.columns:
            detalhe_leads["dt_abertura_ref"] = pd.to_datetime(detalhe_leads["dt_abertura_ref"], errors="coerce").dt.strftime("%d/%m/%Y")
        st.markdown("#### Analítico de prazo do lead")
        render_downloadable_table(detalhe_leads, "leads_analitico_prazo", "leads_analitico_prazo", raw_df=detalhe_leads)

        st.divider()
        st.markdown("### Follow-up diário — leads de 1 a 15 dias")
        st.caption("Base acionável para atuação diária em clientes de 1 a 15 dias.")

        up_prev_msgs = st.file_uploader(
            "Importe os arquivos de mensagens já enviadas (CSV/XLSX)",
            type=["csv", "xlsx"],
            accept_multiple_files=True,
            key="leads_followup_prev_msgs",
        )

        try:
            followup_base = leads_funil.copy()
            if df_panel_visao is not None:
                visao_follow = _extract_visao_base(df_panel_visao)
                visao_follow = visao_follow[[
                    c for c in [
                        "cnpj", "dt_conta_criada", "dt_fundacao_empresa",
                        "telefone", "telefone_master", "pix", "wallet",
                        "fl_propensao_c6pay", "fl_elegivel_venda_c6pay",
                        "status_proposta_sf_pay", "dt_aprovacao_pay",
                        "dt_install_maq", "dt_ativacao_pay", "banco_domicilio"
                    ] if c in visao_follow.columns
                ]].copy()
                followup_base = followup_base.merge(
                    visao_follow,
                    on="cnpj",
                    how="left",
                    suffixes=("", "_visao_follow"),
                )
                for col in [
                    "nome_cliente",
                    "dt_conta_criada",
                    "dt_fundacao_empresa",
                    "telefone",
                    "telefone_master",
                    "pix",
                    "wallet",
                    "fl_propensao_c6pay",
                    "fl_elegivel_venda_c6pay",
                    "status_proposta_sf_pay",
                    "dt_aprovacao_pay",
                    "dt_install_maq",
                    "dt_ativacao_pay",
                    "banco_domicilio",
                ]:
                    visao_col = f"{col}_visao_follow"
                    if visao_col not in followup_base.columns:
                        continue
                    if col not in followup_base.columns:
                        followup_base[col] = followup_base[visao_col]
                        continue
                    left_norm = followup_base[col].apply(_normalize_text_value)
                    fill_mask = left_norm.eq("")
                    followup_base.loc[fill_mask, col] = followup_base.loc[fill_mask, visao_col]

            if "nome_cliente" not in followup_base.columns:
                followup_base["nome_cliente"] = ""
            if "nome_envio" not in followup_base.columns:
                followup_base["nome_envio"] = followup_base["nome_cliente"]
            else:
                followup_base["nome_envio"] = followup_base["nome_envio"].fillna("").replace("", pd.NA).fillna(followup_base["nome_cliente"])
            if "status_abertura_conta" not in followup_base.columns:
                followup_base["status_abertura_conta"] = ""
            if "pendencias" not in followup_base.columns:
                followup_base["pendencias"] = ""
            if "telefone" not in followup_base.columns:
                followup_base["telefone"] = ""
            if "data_hora_cadastro" not in followup_base.columns:
                followup_base["data_hora_cadastro"] = pd.NaT

            ref_followup = pd.to_datetime(followup_base.get("data_base"), errors="coerce").max() if "data_base" in followup_base.columns else pd.NaT
            ref_followup_date = ref_followup.date() if pd.notna(ref_followup) else dt.date.today()
            followup_base["dias_desde_cadastro"] = followup_base["data_hora_cadastro"].apply(
                lambda x: _days_since_today_exclusive(x, ref_followup_date)
            )
            dt_abertura_ref = pd.to_datetime(followup_base.get("dt_abertura_ref", pd.Series(pd.NaT, index=followup_base.index)), errors="coerce")
            dt_conta_criada = pd.to_datetime(followup_base.get("dt_conta_criada", pd.Series(pd.NaT, index=followup_base.index)), errors="coerce")
            followup_base["abriu_conta_flag"] = np.where(dt_abertura_ref.notna() | dt_conta_criada.notna(), "SIM", "NÃO")
            followup_base = followup_base[
                followup_base["dias_desde_cadastro"].apply(lambda x: isinstance(x, (int, np.integer)) and 1 <= int(x) <= 15)
            ].copy()
            followup_base = followup_base[
                followup_base["status_abertura_conta"].fillna("").apply(_is_actionable_followup_status)
            ].copy()
            if "abriu_conta_flag" not in followup_base.columns:
                followup_base["abriu_conta_flag"] = "NÃO"
            followup_base = followup_base[followup_base["abriu_conta_flag"].ne("SIM")].copy()
            if "nome_cliente" not in followup_base.columns:
                followup_base["nome_cliente"] = ""
            if "nome_envio" not in followup_base.columns:
                followup_base["nome_envio"] = followup_base["nome_cliente"]
            else:
                followup_base["nome_envio"] = followup_base["nome_envio"].fillna("").replace("", pd.NA).fillna(followup_base["nome_cliente"])

            hist_prev = _build_previous_message_history(up_prev_msgs)
            nome_envio_series = followup_base["nome_envio"] if "nome_envio" in followup_base.columns else pd.Series([""] * len(followup_base), index=followup_base.index)
            followup_base["nome_key"] = nome_envio_series.fillna("").apply(_normalize_person_key)
            if not hist_prev.empty:
                followup_base = followup_base.merge(hist_prev, on="nome_key", how="left")
            else:
                followup_base["qtde_envios_anteriores"] = 0
                followup_base["ultima_data_envio"] = pd.NaT
                followup_base["ultima_msg_2"] = ""
                followup_base["ultima_msg_3"] = ""
                followup_base["ultima_msg_4"] = ""

            fill_defaults = {
                "qtde_envios_anteriores": 0,
                "ultima_msg_2": "",
                "ultima_msg_3": "",
                "ultima_msg_4": "",
            }
            for col, default_val in fill_defaults.items():
                if col not in followup_base.columns:
                    followup_base[col] = default_val
                else:
                    followup_base[col] = followup_base[col].fillna(default_val)
            if "ultima_data_envio" not in followup_base.columns:
                followup_base["ultima_data_envio"] = pd.NaT

            strategy_rows = pd.DataFrame([_lead_followup_strategy(r) for r in followup_base.to_dict("records")])
            if not strategy_rows.empty:
                followup_base = pd.concat([followup_base.reset_index(drop=True), strategy_rows.reset_index(drop=True)], axis=1)
            else:
                for col in ["foco_dia", "objetivo", "justificativa", "var_2", "var_3", "var_4"]:
                    followup_base[col] = ""

            phone_pairs = followup_base.apply(lambda r: _focus_phone_pair(r), axis=1)
            followup_base["telefone_1"] = [p[0] for p in phone_pairs]
            followup_base["telefone_2"] = [p[1] for p in phone_pairs]
            followup_base["linhas_envio_validas"] = followup_base.apply(
                lambda r: int(bool(r.get("telefone_1"))) + int(bool(r.get("telefone_2"))), axis=1
            )

            mfu1, mfu2, mfu3, mfu4 = st.columns(4)
            mfu1.metric("Clientes acionáveis", br_int(int(len(followup_base))))
            mfu2.metric("Com telefone válido", br_int(int((followup_base["linhas_envio_validas"] > 0).sum())) if not followup_base.empty else "0")
            mfu3.metric("Com envio anterior", br_int(int((pd.to_numeric(followup_base["qtde_envios_anteriores"], errors="coerce").fillna(0) > 0).sum())) if not followup_base.empty else "0")
            mfu4.metric("Linhas de envio", br_int(int(followup_base["linhas_envio_validas"].sum())) if not followup_base.empty else "0")

            analitico_follow = followup_base[[
                c for c in [
                    "nome_envio", "nome_cliente", "cnpj", "data_hora_cadastro", "dias_desde_cadastro",
                    "status_abertura_conta", "pendencias", "telefone_1", "telefone_2",
                    "qtde_envios_anteriores", "ultima_data_envio", "foco_dia",
                    "objetivo", "justificativa", "dt_fundacao_empresa", "dt_conta_criada"
                ] if c in followup_base.columns
            ]].copy()
            if not analitico_follow.empty:
                if "data_hora_cadastro" in analitico_follow.columns:
                    analitico_follow["data_hora_cadastro"] = pd.to_datetime(analitico_follow["data_hora_cadastro"], errors="coerce").dt.strftime("%d/%m/%Y %H:%M")
                if "ultima_data_envio" in analitico_follow.columns:
                    analitico_follow["ultima_data_envio"] = pd.to_datetime(analitico_follow["ultima_data_envio"], errors="coerce").dt.strftime("%d/%m/%Y")
                if "dt_fundacao_empresa" in analitico_follow.columns:
                    analitico_follow["dt_fundacao_empresa"] = pd.to_datetime(analitico_follow["dt_fundacao_empresa"], errors="coerce").dt.strftime("%d/%m/%Y")
                if "dt_conta_criada" in analitico_follow.columns:
                    analitico_follow["dt_conta_criada"] = pd.to_datetime(analitico_follow["dt_conta_criada"], errors="coerce").dt.strftime("%d/%m/%Y")
            analitico_follow = analitico_follow.rename(columns={
                "nome_envio": "nome do cliente",
                "nome_cliente": "nome empresa",
                "cnpj": "CNPJ",
                "data_hora_cadastro": "data do cadastro",
                "dias_desde_cadastro": "dias desde o cadastro",
                "status_abertura_conta": "status atual",
                "pendencias": "pendências",
                "dt_fundacao_empresa": "data de fundação da empresa",
                "dt_conta_criada": "data de conta criada",
                "qtde_envios_anteriores": "envios anteriores",
                "ultima_data_envio": "última data de envio",
                "foco_dia": "foco do dia",
            })
            st.markdown("#### Analítico acionável — 1 a 15 dias")
            render_downloadable_table(
                analitico_follow,
                "leads_followup_1a15",
                "leads_followup_1a15_dias",
                raw_df=analitico_follow,
            )

            envio_msg_rows = []
            clientes_ura_rows = []
            for row in followup_base.to_dict("records"):
                telefones_com_55 = []
                telefones_sem_55 = []
                for raw_phone in [row.get("telefone_1", ""), row.get("telefone_2", "")]:
                    phone_55 = re.sub(r"\D+", "", str(raw_phone or ""))
                    if phone_55 and not phone_55.startswith("55"):
                        phone_55 = f"55{phone_55}"
                    phone_sem_55 = phone_55[2:] if phone_55.startswith("55") and len(phone_55) > 11 else phone_55
                    if phone_55 and phone_55 not in telefones_com_55:
                        telefones_com_55.append(phone_55)
                    if phone_sem_55 and phone_sem_55 not in telefones_sem_55:
                        telefones_sem_55.append(phone_sem_55)
                for phone_55 in telefones_com_55:
                    envio_msg_rows.append({
                        "telefone com 55": phone_55,
                        "nome do cliente": row.get("nome_envio", row.get("nome_cliente", "")),
                        "variável 2": row.get("var_2", ""),
                        "variável 3": row.get("var_3", ""),
                        "variável 4": row.get("var_4", ""),
                    })
                phone_sem_55 = telefones_sem_55[0] if telefones_sem_55 else ""
                if phone_sem_55:
                    clientes_ura_rows.append({
                        "Nome": row.get("nome_cliente", row.get("nome_envio", "")),
                        "CNPJ": re.sub(r"\D+", "", str(row.get("cnpj", "") or "")),
                        "TELEFONE1": phone_sem_55,
                        "TELEFONE2": "",
                    })

            envio_msg_df = pd.DataFrame(envio_msg_rows)
            clientes_ura_df = pd.DataFrame(clientes_ura_rows)
            st.markdown("#### Arquivos de envio do dia")
            if envio_msg_df.empty and clientes_ura_df.empty:
                if not followup_base.empty and int(followup_base["linhas_envio_validas"].sum()) == 0:
                    st.warning("Há clientes acionáveis entre 1 e 15 dias, mas nenhum telefone válido foi encontrado na coluna G do Analítico Leads.")
                else:
                    st.info("Não há clientes acionáveis entre 1 e 15 dias na base atual.")
            else:
                if not envio_msg_df.empty:
                    st.markdown("##### Arquivo de mensagens")
                    st.dataframe(envio_msg_df, use_container_width=True, hide_index=True)
                    csv_msg_text = envio_msg_df.to_csv(index=False, header=False, sep=";", lineterminator="\n")
                    csv_msg_bytes = csv_msg_text.encode("utf-8-sig")
                    data_base_leads = pd.to_datetime(followup_base.get("data_base"), errors="coerce").max() if "data_base" in followup_base.columns else pd.NaT
                    data_base_txt = data_base_leads.strftime("%d%m%Y") if pd.notna(data_base_leads) else dt.date.today().strftime("%d%m%Y")
                    data_envio_txt = dt.date.today().strftime("%d%m%Y")
                    followup_name = f"BOTXXXX_{data_base_txt}_{data_envio_txt}_INDICADOS_TOTAIS_1A15DIAS.csv"
                    st.download_button(
                        "Baixar arquivo de mensagens",
                        data=csv_msg_bytes,
                        file_name=followup_name,
                        mime="text/csv",
                        key="dl_leads_followup_envio_csv",
                        help="Baixar CSV UTF-8 do follow-up 1 a 15 dias",
                    )
                if not clientes_ura_df.empty:
                    st.markdown("##### clientes um a 15 dias URa")
                    st.caption("Importe uma blacklist com CNPJ e/ou telefone para excluir clientes deste arquivo. O CNPJ pode vir com ou sem máscara; telefone pode vir com ou sem 55.")
                    blacklist_upload = st.file_uploader(
                        "Blacklist URA 1 a 15 dias (CSV/XLSX)",
                        type=["csv", "xlsx"],
                        accept_multiple_files=False,
                        key="leads_ura_1a15_blacklist",
                    )
                    gerar_sem_blacklist = st.checkbox("Não tenho blacklist para este envio", key="leads_ura_1a15_sem_blacklist")
                    clientes_ura_filtrado = pd.DataFrame()
                    if blacklist_upload is not None:
                        try:
                            blacklist_df = _read_blacklist_upload(blacklist_upload)
                            blacklist_cnpjs, blacklist_phones = _blacklist_sets_from_df(blacklist_df)
                            clientes_ura_filtrado = _apply_ura_blacklist(clientes_ura_df, blacklist_cnpjs, blacklist_phones)
                            removidos = len(clientes_ura_df) - len(clientes_ura_filtrado)
                            st.caption(f"Blacklist aplicada: {br_int(len(blacklist_cnpjs))} CNPJ(s), {br_int(len(blacklist_phones))} telefone(s), {br_int(removidos)} linha(s) removida(s).")
                        except Exception as exc:
                            st.error(f"Não consegui ler a blacklist: {exc}")
                    elif gerar_sem_blacklist:
                        clientes_ura_filtrado = clientes_ura_df.copy()
                        st.caption("Gerando sem blacklist.")
                    else:
                        st.info("Relatório disponível após importar a blacklist ou marcar que não existe blacklist.")

                    if not clientes_ura_filtrado.empty:
                        st.dataframe(clientes_ura_filtrado, use_container_width=True, hide_index=True)
                        csv_ura_text = clientes_ura_filtrado.to_csv(index=False, header=True, sep=";", lineterminator="\n")
                        csv_ura_bytes = csv_ura_text.encode("utf-8-sig")
                        st.download_button(
                            "Baixar clientes 1 a 15 dias URA",
                            data=csv_ura_bytes,
                            file_name="clientes um a 15 dias URa.csv",
                            mime="text/csv",
                            key="dl_leads_followup_clientes_ura_csv",
                            help="Baixar CSV UTF-8 dos clientes de 1 a 15 dias após blacklist",
                        )
        except Exception as exc:
            st.error(f"Não consegui montar o follow-up diário de 1 a 15 dias: {exc}")

        df_lct_for_ura = df_ura_lct if 'df_ura_lct' in locals() and df_ura_lct is not None and not df_ura_lct.empty else df_panel_lct
        if 'df_lct_for_ura' in locals() and df_lct_for_ura is not None and not df_lct_for_ura.empty:
            st.divider()
            st.markdown("### Clientes da URA / Saber Mais")
            leads_ura_funil = _load_funil_history_from_temp_imports("leads", _extract_leads_base)
            visao_ura_funil = _load_funil_history_from_temp_imports("visao", _extract_visao_base)
            if not leads_funil.empty:
                leads_ura_funil = pd.concat([leads_ura_funil, _extract_leads_base(df_panel_leads)], ignore_index=True) if not leads_ura_funil.empty else _extract_leads_base(df_panel_leads)
                leads_ura_funil = leads_ura_funil.sort_values([c for c in ["cnpj", "data_base", "data_hora_cadastro"] if c in leads_ura_funil.columns]).drop_duplicates("cnpj", keep="last")
            if df_panel_visao is not None and not df_panel_visao.empty:
                visao_current_ura = _extract_visao_base(df_panel_visao)
                visao_ura_funil = pd.concat([visao_ura_funil, visao_current_ura], ignore_index=True) if not visao_ura_funil.empty else visao_current_ura
                visao_ura_funil = visao_ura_funil.sort_values([c for c in ["cnpj", "data_base", "dt_conta_criada"] if c in visao_ura_funil.columns]).drop_duplicates("cnpj", keep="last")
            if leads_ura_funil.empty:
                leads_ura_funil = leads_funil
            if visao_ura_funil.empty:
                visao_ura_funil = visao_funil

            def _render_lct_source(label: str, view_df: pd.DataFrame, analitico_df: pd.DataFrame, key_prefix: str):
                if view_df.empty:
                    st.info(f"O Resumo LCT foi lido, mas não encontrei clientes {label} com CNPJ e Data.")
                    return
                st.markdown(f"#### Clientes {label}")
                view_show = view_df.copy()
                count_cols = [c for c in view_show.columns if c.startswith("Clientes ")] + ["Indicados no banco", "Abriram conta"]
                for col in count_cols:
                    if col in view_show.columns:
                        view_show[col] = pd.to_numeric(view_show[col], errors="coerce").fillna(0).astype(int).apply(br_int)
                for col in ["% viraram indicação", "% abriram conta"]:
                    if col in view_show.columns:
                        view_show[col] = view_show[col].apply(lambda x: f"{float(x):.1f}%".replace(".", ","))
                render_downloadable_table(view_show, f"{key_prefix}_resumo", f"{key_prefix}_resumo", raw_df=view_df)
                render_downloadable_table(analitico_df, f"{key_prefix}_analitico", f"{key_prefix}_analitico", raw_df=analitico_df)

            view_ura, analitico_ura = _build_ura_reports(df_lct_for_ura, leads_ura_funil, visao_ura_funil)
            view_sbm, analitico_sbm = _build_sbm_reports(df_lct_for_ura, leads_ura_funil, visao_ura_funil)
            _render_lct_source("URA", view_ura, analitico_ura, "leads_clientes_ura")
            _render_lct_source("SBM / Saber Mais", view_sbm, analitico_sbm, "leads_clientes_sbm")
        elif 'df_lct_for_ura' in locals() and df_lct_for_ura is not None and df_lct_for_ura.empty:
            st.info("O Resumo LCT foi carregado, mas veio vazio.")

    if df_panel_visao is not None:
        visao_funil = _extract_visao_base(df_panel_visao)
        track = local_json_load(C6_DAILY_FUNIL_TRACK, default={}) or {}
        if not visao_funil.empty:
            track_df = pd.DataFrame([
                {
                    "cnpj": cnpj,
                    "pix_primeira_aparicao": _parse_br_date_text(item.get("pix_primeira_aparicao")),
                    "pix_cnpj_primeira_aparicao": _parse_br_date_text(item.get("pix_cnpj_primeira_aparicao")),
                    "wallet_primeira_aparicao": _parse_br_date_text(item.get("wallet_primeira_aparicao")),
                    "c6pay_ativa30_primeira_aparicao": _parse_br_date_text(item.get("c6pay_ativa30_primeira_aparicao")),
                }
                for cnpj, item in track.items() if isinstance(item, dict)
            ])
            if not track_df.empty:
                visao_funil = visao_funil.merge(track_df, on="cnpj", how="left")
            else:
                visao_funil["pix_primeira_aparicao"] = pd.NaT
                visao_funil["pix_cnpj_primeira_aparicao"] = pd.NaT
                visao_funil["wallet_primeira_aparicao"] = pd.NaT
                visao_funil["c6pay_ativa30_primeira_aparicao"] = pd.NaT

            visao_funil["dias_abertura_pix"] = visao_funil.apply(
                lambda r: _elapsed_days_inclusive(
                    r.get("dt_conta_criada"),
                    r.get("pix_primeira_aparicao") if pd.notna(r.get("pix_primeira_aparicao")) else r.get("data_base")
                ),
                axis=1
            )
            visao_funil["dias_abertura_pix_cnpj"] = visao_funil.apply(
                lambda r: _elapsed_days_inclusive(
                    r.get("dt_conta_criada"),
                    r.get("pix_cnpj_primeira_aparicao") if pd.notna(r.get("pix_cnpj_primeira_aparicao")) else r.get("data_base")
                ),
                axis=1
            )
            visao_funil["dias_entrega_ativ_cartao"] = visao_funil.apply(lambda r: _elapsed_days_inclusive(r.get("dt_entrega_cartao"), r.get("dt_ativ_cartao_cred")), axis=1)
            visao_funil["dias_abertura_aprov_pay"] = visao_funil.apply(lambda r: _elapsed_days_inclusive(r.get("dt_conta_criada"), r.get("dt_aprovacao_pay")), axis=1)
            visao_funil["dias_aprov_install_pay"] = visao_funil.apply(lambda r: _elapsed_days_inclusive(r.get("dt_aprovacao_pay"), r.get("dt_install_maq")), axis=1)
            visao_funil["dias_install_ativ_pay"] = visao_funil.apply(lambda r: _elapsed_days_inclusive(r.get("dt_install_maq"), r.get("dt_ativacao_pay")), axis=1)
            visao_funil["dias_abertura_ativ_pay"] = visao_funil.apply(lambda r: _elapsed_days_inclusive(r.get("dt_conta_criada"), r.get("dt_ativacao_pay")), axis=1)

            def _avg(series_name: str) -> float:
                s = pd.to_numeric(visao_funil[series_name], errors="coerce").dropna()
                return float(s.mean()) if not s.empty else 0.0

            st.divider()
            st.markdown("### Tempos médios do cliente após a abertura")
            v1, v2, v3, v4 = st.columns(4)
            v1.metric("Abertura → Pix", f"{_avg('dias_abertura_pix'):.1f} dias".replace(".", ","))
            v2.metric("Abertura → Pix CNPJ", f"{_avg('dias_abertura_pix_cnpj'):.1f} dias".replace(".", ","))
            v3.metric("Entrega → ativação cartão", f"{_avg('dias_entrega_ativ_cartao'):.1f} dias".replace(".", ","))
            v4.metric("Abertura → aprovação Pay", f"{_avg('dias_abertura_aprov_pay'):.1f} dias".replace(".", ","))

            v5, v6, v7, v8 = st.columns(4)
            v5.metric("Aprovação → instalação", f"{_avg('dias_aprov_install_pay'):.1f} dias".replace(".", ","))
            v6.metric("Instalação → ativação Pay", f"{_avg('dias_install_ativ_pay'):.1f} dias".replace(".", ","))
            v7.metric("Abertura → ativação Pay", f"{_avg('dias_abertura_ativ_pay'):.1f} dias".replace(".", ","))
            v8.metric("C6 Pay ativa 30", br_int(int(visao_funil["c6pay_ativa_30"].astype(str).str.upper().isin(["1", "SIM", "TRUE", "S"]).sum())))

            qtd_aberta = int(visao_funil["dt_conta_criada"].notna().sum())
            qtd_pix = int(visao_funil["pix"].apply(_pix_is_valid).sum())
            qtd_pix_cnpj = int(visao_funil["pix"].apply(_pix_has_cnpj).sum())
            qtd_cartao_entregue = int(visao_funil["dt_entrega_cartao"].notna().sum())
            qtd_cartao_ativado = int(visao_funil["dt_ativ_cartao_cred"].notna().sum())
            qtd_pay_aprov = int(visao_funil["dt_aprovacao_pay"].notna().sum())
            qtd_pay_inst = int(visao_funil["dt_install_maq"].notna().sum())
            qtd_pay_ativ = int(visao_funil["dt_ativacao_pay"].notna().sum())
            fases_rows = [
                {"Etapa": "Clientes com conta aberta", "Quantidade": qtd_aberta, "% avanço": 100.0 if qtd_aberta > 0 else 0.0},
                {"Etapa": "Clientes com Pix", "Quantidade": qtd_pix, "% avanço": (qtd_pix / qtd_aberta * 100.0) if qtd_aberta > 0 else 0.0},
                {"Etapa": "Clientes com Pix CNPJ", "Quantidade": qtd_pix_cnpj, "% avanço": (qtd_pix_cnpj / qtd_pix * 100.0) if qtd_pix > 0 else 0.0},
                {"Etapa": "Cartão entregue", "Quantidade": qtd_cartao_entregue, "% avanço": (qtd_cartao_entregue / qtd_aberta * 100.0) if qtd_aberta > 0 else 0.0},
                {"Etapa": "Cartão ativado", "Quantidade": qtd_cartao_ativado, "% avanço": (qtd_cartao_ativado / qtd_cartao_entregue * 100.0) if qtd_cartao_entregue > 0 else 0.0},
                {"Etapa": "C6 Pay aprovado", "Quantidade": qtd_pay_aprov, "% avanço": (qtd_pay_aprov / qtd_aberta * 100.0) if qtd_aberta > 0 else 0.0},
                {"Etapa": "C6 Pay instalado", "Quantidade": qtd_pay_inst, "% avanço": (qtd_pay_inst / qtd_pay_aprov * 100.0) if qtd_pay_aprov > 0 else 0.0},
                {"Etapa": "C6 Pay ativado", "Quantidade": qtd_pay_ativ, "% avanço": (qtd_pay_ativ / qtd_pay_inst * 100.0) if qtd_pay_inst > 0 else 0.0},
            ]
            df_fases = pd.DataFrame(fases_rows)
            df_fases["Quantidade"] = df_fases["Quantidade"].apply(br_int)
            df_fases["% avanço"] = df_fases["% avanço"].apply(lambda x: f"{x:.1f}%".replace(".", ","))
            st.markdown("#### Avanço do cliente no funil")
            render_downloadable_table(df_fases, "leads_funil_avanco", "leads_funil_avanco", raw_df=df_fases)

            df_visao_tempo = pd.DataFrame([
                {"Etapa": "Abertura → Pix", "Média (dias)": _avg("dias_abertura_pix"), "Base de clientes": int(pd.to_numeric(visao_funil["dias_abertura_pix"], errors="coerce").notna().sum())},
                {"Etapa": "Abertura → Pix CNPJ", "Média (dias)": _avg("dias_abertura_pix_cnpj"), "Base de clientes": int(pd.to_numeric(visao_funil["dias_abertura_pix_cnpj"], errors="coerce").notna().sum())},
                {"Etapa": "Entrega → ativação cartão", "Média (dias)": _avg("dias_entrega_ativ_cartao"), "Base de clientes": int(pd.to_numeric(visao_funil["dias_entrega_ativ_cartao"], errors="coerce").notna().sum())},
                {"Etapa": "Abertura → aprovação Pay", "Média (dias)": _avg("dias_abertura_aprov_pay"), "Base de clientes": int(pd.to_numeric(visao_funil["dias_abertura_aprov_pay"], errors="coerce").notna().sum())},
                {"Etapa": "Aprovação → instalação", "Média (dias)": _avg("dias_aprov_install_pay"), "Base de clientes": int(pd.to_numeric(visao_funil["dias_aprov_install_pay"], errors="coerce").notna().sum())},
                {"Etapa": "Instalação → ativação Pay", "Média (dias)": _avg("dias_install_ativ_pay"), "Base de clientes": int(pd.to_numeric(visao_funil["dias_install_ativ_pay"], errors="coerce").notna().sum())},
                {"Etapa": "Abertura → ativação Pay", "Média (dias)": _avg("dias_abertura_ativ_pay"), "Base de clientes": int(pd.to_numeric(visao_funil["dias_abertura_ativ_pay"], errors="coerce").notna().sum())},
            ])
            df_visao_tempo["Média (dias)"] = df_visao_tempo["Média (dias)"].apply(lambda x: f"{x:.1f}".replace(".", ","))
            df_visao_tempo["Base de clientes"] = df_visao_tempo["Base de clientes"].apply(br_int)
            st.markdown("#### Médias de tempo do funil")
            render_downloadable_table(df_visao_tempo, "leads_funil_tempo", "leads_funil_tempo", raw_df=df_visao_tempo)

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

            st.markdown("### Resumo geral")
            col_metric1, col_metric2 = st.columns(2)
            col_metric3, col_metric4 = st.columns(2)

            dias_unicos = int(dfh["Data"].nunique())
            status_unicos = int(dfh["Status"].nunique())

            last_info = control.get("last_processed", {}) or {}
            last_day = last_info.get("day")

            if not last_day:
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
                st.metric("Total de leads", br_int(total_geral))
            with col_metric4:
                st.metric("Indicações válidas até 14 dias", br_int(total_validas))

            st.divider()

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

            df_mes = dfh[dfh["Mes"] == mes_sel].copy()

            if not df_mes.empty:
                st.markdown("### Comparativo diário")

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
                    styled = styled.map(color_delta, subset=delta_cols_display)

                if 'Total' in view_display.columns:
                    styled = styled.map(lambda x: 'font-weight: 900;', subset=['Total'])

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

                render_downloadable_table(styled, "leads_status_hist", "leads_status_hist", raw_df=view_display)

    st.divider()
    col_reset1, col_reset2, col_reset3 = st.columns([1, 2, 1])
    with col_reset2:
        if st.button("Resetar somente Leads - Status Diário", use_container_width=True, type="secondary"):
            _leads_status_reset_only()

if "Mensagens" in tabs_map:
  with tabs_map["Mensagens"]:
    st.header("Mensagens, Indicações e Abertura")
    st.caption("Geração de bases UTF-8 para envio oficial, usando os arquivos já importados no Painel C6 Empresas.")

    _, msg_tabs_map = _single_visible_tab(["Abertura", "Ura Lemit"], "mensagens_subtab", default="Abertura")

    if "Abertura" in msg_tabs_map:
      with msg_tabs_map["Abertura"]:
        st.subheader("Relatório para ações - contas abertas")
        df_visao_msg, visao_msg_name, _ = _load_daily_import_cache("visao")

        if df_visao_msg is None or df_visao_msg.empty:
          st.info("Importe primeiro a planilha C6 (Visão Cliente) no Painel C6 Empresas.")
        else:
          acoes_abertas_df = _build_open_accounts_actions_report(df_visao_msg)
          acoes_summary = _open_accounts_actions_summary(df_visao_msg)
          st.caption(f"Visão Cliente em uso: {visao_msg_name or 'arquivo importado'}")
          if acoes_abertas_df.empty:
            st.info("Nenhum cliente com conta liberada/aberta encontrado para o relatório de ações.")
          else:
            latest_open = pd.to_datetime(acoes_abertas_df["dt_conta_criada"], errors="coerce", dayfirst=True).max()
            file_date = latest_open.strftime("%d%m%Y") if pd.notna(latest_open) else dt.date.today().strftime("%d%m%Y")
            st.download_button(
              "Baixar relatório para ações - contas abertas (Excel)",
              data=_to_excel_bytes({"Contas_Abertas": acoes_abertas_df}),
              file_name=f"relatorio_acoes_contas_abertas_{file_date}.xlsx",
              mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
              use_container_width=True,
            )
            st.caption(
              f"Base importada: {br_int(acoes_summary['linhas_base'])} clientes. "
              f"Linhas potenciais de telefone: {br_int(acoes_summary['linhas_telefone_brutas'])}. "
              f"Excluídos por status bloqueado/desativado/encerrado/cancelado: {br_int(acoes_summary['excluidas_status'])}. "
              f"Telefones duplicados no mesmo cliente removidos: {br_int(acoes_summary['telefones_duplicados_cliente'])}. "
              f"Clientes sem telefone: {br_int(acoes_summary['sem_telefone'])}. "
              f"No arquivo: {br_int(acoes_abertas_df['cnpj'].nunique())} clientes e {br_int(len(acoes_abertas_df))} linhas de telefone."
            )

          st.divider()
          st.subheader("Abertura - clientes com até 4 dias")

          def _msg_col(df: pd.DataFrame, names: List[str], index_zero_based: Optional[int] = None) -> Optional[str]:
            col = _coalesce_col(df, names)
            if col is not None:
              return col
            if index_zero_based is not None and len(df.columns) > index_zero_based:
              return df.columns[index_zero_based]
            return None

          def _msg_series(df: pd.DataFrame, names: List[str], index_zero_based: Optional[int] = None):
            col = _msg_col(df, names, index_zero_based)
            if col is None:
              return pd.Series([pd.NA] * len(df), index=df.index)
            return df[col]

          def _msg_phone(v) -> str:
            digits = re.sub(r"\D+", "", str(v or ""))
            if not digits:
              return ""
            if digits.startswith("55"):
              return digits
            return "55" + digits

          df_msg = df_visao_msg.copy()
          base_s = to_date_series(_msg_series(df_msg, [COL_DATA_BASE, "DATA BASE", "DT_BASE"], 1))
          abertura_s = to_date_series(_msg_series(df_msg, [COL_ABERTURA, "DATA CONTA CRIADA", "DT CONTA CRIADA"], 19))
          nome_s = _msg_series(df_msg, ["NOME_CLIENTE", "NOME CLIENTE", "CLIENTE", "NOME"], 3).astype("string").fillna("")
          cnpj_s = _msg_series(df_msg, [COL_CNPJ, "CNPJ", "CPF_CNPJ"], 2).astype("string").fillna("")
          tel_1_s = _msg_series(df_msg, ["TELEFONE", "TEL", "FONE", "TELEFONE_1"], 12)
          tel_2_s = _msg_series(df_msg, ["TELEFONE_2", "TEL2", "FONE2", "CELULAR"], 13)
          status_s = normalize_str(_msg_series(df_msg, [COL_STATUS, "STATUS", "STATUS_CONTA"], 21))
          pix_s = _msg_series(df_msg, [COL_PIX, "PIX", "CHAVE_PIX", "CHAVES PIX FORTE"], 23)
          criterio_s = normalize_str(_msg_series(df_msg, [COL_CRIT, "CRITERIOS_ATINGIDOS_COMISS", "CRITÉRIOS ATINGIDOS COMISS"], 76))

          work = pd.DataFrame({
            "data_base": base_s,
            "data_abertura": abertura_s,
            "nome": nome_s,
            "cnpj": cnpj_s,
            "telefone_1": tel_1_s,
            "telefone_2": tel_2_s,
            "status": status_s,
            "pix": pix_s,
            "criterio": criterio_s,
          })

          work["dias_conta"] = (pd.to_datetime(work["data_base"], errors="coerce") - pd.to_datetime(work["data_abertura"], errors="coerce")).dt.days + 1
          work["tem_pix_cnpj"] = work["pix"].apply(_pix_has_cnpj)
          work["apto_status"] = work["status"].str.contains("LIBERADA", na=False) & ~work["status"].str.contains("BLOQUEAD|DESATIVAD|ENCERRAD", na=False)
          work["apto_criterio"] = work["criterio"].str.contains("CASH", na=False) & work["criterio"].str.contains("IN", na=False) & work["criterio"].str.contains(r"\d", regex=True, na=False)
          work["cliente_mei"] = work["criterio"].str.contains("CLIENTE MEI| MEI", na=False)

          base_filtrada = work[
            work["data_base"].notna()
            & work["data_abertura"].notna()
            & work["dias_conta"].between(1, 4, inclusive="both")
            & work["apto_status"]
            & work["apto_criterio"]
            & ~work["cliente_mei"]
          ].copy()

          csv_rows = []
          preview_rows = []
          for _, row in base_filtrada.iterrows():
            telefones = []
            for raw_phone in [row.get("telefone_1"), row.get("telefone_2")]:
              phone = _msg_phone(raw_phone)
              if phone and phone not in telefones:
                telefones.append(phone)

            data_txt = fmt_date(row["data_abertura"]).replace("/", ".")
            msg_1 = f"Parabéns pela abertura da sua conta no C6 Empresas em {data_txt} — sua empresa já está com a conta ativa. 🎉"
            if row["tem_pix_cnpj"]:
              msg_2 = "Com o Pix ativo, você já pode começar a vender e movimentar com mais eficiência. Uma ótima opção é a C6 Pay, com taxas competitivas, Pix gratuito nas vendas e recebimento direto na conta."
              msg_3 = "Além disso, você pode ter acesso ao cartão de crédito empresarial e, se necessário, utilizar o CDB Cartão de Crédito, que permite até R$ 200 mil de limite enquanto o valor aplicado rende cerca de 102% do CDI, sujeito às condições do banco."
              tipo = "Com Pix CNPJ"
            else:
              msg_2 = "Identificamos que a chave Pix CNPJ ainda não foi cadastrada. Ativar o Pix é essencial para começar a movimentar a conta, receber sem custo e utilizar melhor os recursos do banco."
              msg_3 = "Além disso, contas com movimentação ativa tendem a ser consideradas nas análises internas de crédito, podendo facilitar o acesso a limites e soluções financeiras, conforme avaliação."
              tipo = "Sem Pix CNPJ"

            for phone in telefones:
              csv_rows.append([phone, row["nome"], msg_1, msg_2, msg_3])
              preview_rows.append({
                "telefone": phone,
                "nome": row["nome"],
                "cnpj": row["cnpj"],
                "data_abertura": fmt_date(row["data_abertura"]),
                "dias_conta": int(row["dias_conta"]),
                "tipo": tipo,
              })

          c1, c2, c3 = st.columns(3)
          c1.metric("Clientes aptos", br_int(len(base_filtrada)))
          c2.metric("Linhas para envio", br_int(len(csv_rows)))
          c3.metric("Com Pix CNPJ", br_int(int(base_filtrada["tem_pix_cnpj"].sum()) if not base_filtrada.empty else 0))

          if not csv_rows:
            st.info("Nenhum cliente apto encontrado para abertura de 1 a 4 dias com as regras atuais.")
          else:
            df_csv = pd.DataFrame(csv_rows)
            csv_bytes = df_csv.to_csv(index=False, header=False, sep=";").encode("utf-8-sig")
            latest_base = pd.to_datetime(base_filtrada["data_base"], errors="coerce").max()
            file_date = latest_base.strftime("%d%m%Y") if pd.notna(latest_base) else dt.date.today().strftime("%d%m%Y")
            st.download_button(
              "Baixar arquivo de abertura (CSV)",
              data=csv_bytes,
              file_name=f"BOT4431_C6EMPRESAS_{file_date}_ASSISEMOLLERKE_ABERTAS_RECENTES.csv",
              mime="text/csv",
              use_container_width=True,
            )
            render_downloadable_table(
              pd.DataFrame(preview_rows),
              "mensagens_abertura_preview",
              "mensagens_abertura_preview",
              raw_df=pd.DataFrame(preview_rows),
            )

    if "Ura Lemit" in msg_tabs_map:
      with msg_tabs_map["Ura Lemit"]:
        st.subheader("Ura Lemit")
        st.caption("Geração da base URA no layout Nome, CNPJ, TELEFONE1 e TELEFONE2.")

        up_ura_limit = st.file_uploader(
          "Arquivo C6BANK telefones (.csv)",
          type=["csv"],
          key="ura_limit_upload",
        )

        def _read_csv_semicolon(uploaded_file):
          raw = uploaded_file.getvalue()
          for enc in ["utf-8-sig", "utf-8", "latin1"]:
            try:
              return pd.read_csv(io.BytesIO(raw), sep=";", dtype="string", encoding=enc, index_col=False)
            except Exception:
              continue
          return pd.read_csv(io.BytesIO(raw), sep=None, engine="python", dtype="string", index_col=False)

        def _only_digits(v) -> str:
          return re.sub(r"\D+", "", str(v or ""))

        def _phone_without_country_code(v) -> str:
          phone = _only_digits(v)
          if phone.startswith("55") and len(phone) > 11:
            phone = phone[2:]
          return phone

        def _clean_col_key(v) -> str:
          txt = str(v or "").strip().upper()
          txt = unicodedata.normalize("NFKD", txt).encode("ascii", "ignore").decode("ascii")
          return re.sub(r"\s+", " ", txt).strip()

        def _find_col_required(df: pd.DataFrame, options: List[str]) -> Optional[str]:
          cols = {}
          for c in df.columns:
            key = _clean_col_key(c)
            if key not in cols:
              cols[key] = c
          for opt in options:
            key = _clean_col_key(opt)
            if key in cols:
              return cols[key]
          return None

        if up_ura_limit is not None:
          try:
            df_ura_limit = _read_csv_semicolon(up_ura_limit)
          except Exception as e:
            st.error("Não consegui ler o CSV. Confira se o arquivo está separado por ponto e vírgula.")
            st.exception(e)
            df_ura_limit = pd.DataFrame()

          if not df_ura_limit.empty:
            nome_col = _find_col_required(df_ura_limit, ["NOME COMPLETO", "NOME", "CLIENTE"])
            cnpj_col = _find_col_required(df_ura_limit, ["CNPJ", "CPF_CNPJ", "CD_CPF_CNPJ_CLIENTE"])
            tel_col = _find_col_required(df_ura_limit, ["TELEFONE", "FONE", "CELULAR"])
            venc_col = _find_col_required(df_ura_limit, ["VENCIMENTO", "VENC_CONT", "DATA VENCIMENTO"])

            missing = []
            if not nome_col:
              missing.append("NOME COMPLETO")
            if not cnpj_col:
              missing.append("CNPJ")
            if not tel_col:
              missing.append("TELEFONE")
            if not venc_col:
              missing.append("VENCIMENTO")

            if missing:
              st.warning("Arquivo inválido. Não encontrei as colunas obrigatórias: " + ", ".join(missing) + ".")
            else:
              work_ura = df_ura_limit.copy()
              work_ura["_cnpj_limpo"] = work_ura[cnpj_col].apply(_only_digits)
              work_ura["_telefone_limpo"] = work_ura[tel_col].apply(_phone_without_country_code)
              work_ura["_nome"] = work_ura[nome_col].astype("string").fillna("").str.strip()
              work_ura["_vencimento"] = pd.to_datetime(work_ura[venc_col], errors="coerce", dayfirst=True)
              work_ura = work_ura[(work_ura["_cnpj_limpo"] != "") & (work_ura["_telefone_limpo"] != "")].copy()

              seen_cnpj = set()
              seen_phone = set()
              grouped = []

              for _, row in work_ura.iterrows():
                cnpj = row["_cnpj_limpo"]
                phone = row["_telefone_limpo"]
                if phone in seen_phone:
                  continue

                target = None
                for item in grouped:
                  if item["CNPJ"] == cnpj:
                    target = item
                    break

                if target is None:
                  if cnpj in seen_cnpj:
                    continue
                  target = {
                    "Nome": row["_nome"],
                    "CNPJ": cnpj,
                    "TELEFONE1": "",
                    "TELEFONE2": "",
                  }
                  grouped.append(target)
                  seen_cnpj.add(cnpj)

                if not target["TELEFONE1"]:
                  target["TELEFONE1"] = phone
                  seen_phone.add(phone)
                elif not target["TELEFONE2"]:
                  target["TELEFONE2"] = phone
                  seen_phone.add(phone)

              out_ura = pd.DataFrame(grouped, columns=["Nome", "CNPJ", "TELEFONE1", "TELEFONE2"])
              venc_validas = work_ura["_vencimento"].dropna()
              if not venc_validas.empty:
                inicio = venc_validas.min().strftime("%d%m%Y")
                fim = venc_validas.max().strftime("%d%m%Y")
              else:
                inicio = dt.date.today().strftime("%d%m%Y")
                fim = inicio
              geracao = dt.date.today().strftime("%d%m%Y")
              file_name = f"{inicio}A{fim}_URA_ENVIO_{geracao}_appassisemollerke.csv"
              csv_out = out_ura.to_csv(index=False, sep=";").encode("utf-8-sig")

              c1, c2, c3 = st.columns(3)
              c1.metric("Linhas importadas", br_int(len(df_ura_limit)))
              c2.metric("CNPJs únicos", br_int(len(out_ura)))
              c3.metric("Telefones únicos usados", br_int(sum(1 for item in grouped for col in ["TELEFONE1", "TELEFONE2"] if item.get(col))))

              st.download_button(
                "Baixar base URA (CSV)",
                data=csv_out,
                file_name=file_name,
                mime="text/csv",
                use_container_width=True,
              )
              st.dataframe(out_ura.head(200), use_container_width=True, hide_index=True)
