# =========================
# app.py — COMPLETO
# =========================

import os
import io
import json
import re
import hashlib
import smtplib
import unicodedata
import datetime as dt
from typing import Dict, Tuple, Optional, List
from email.message import EmailMessage

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
ACELERADORES_NOVA = [
    (4000, 1.50),
    (2000, 1.25),
    (1000, 1.10),
    (0, 1.00),
]

CARTILHA_NOVA_MESES = {"04/2026", "05/2026", "06/2026"}
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

DATA_DIR = "data_store"
os.makedirs(DATA_DIR, exist_ok=True)

HIST_OPEN_DAILY = os.path.join(DATA_DIR, "hist_aberturas_diario.json")        # dd/mm/aaaa -> aberturas
HIST_LEADS_DAILY = os.path.join(DATA_DIR, "hist_cadastros_diario.json")       # dd/mm/aaaa -> cadastradas
HIST_MONTH_LEVELS = os.path.join(DATA_DIR, "hist_mes_cnpj_nivel.json")        # mm/aaaa -> {cnpj: nivel_max_no_mes}
HIST_PAGO_POR_CNPJ = os.path.join(DATA_DIR, "pago_max_por_cnpj.json")         # cnpj -> max pago acumulado
HIST_RESUMO_MENSAL = os.path.join(DATA_DIR, "resumo_mensal.json")             # mm/aaaa -> resumo calculado
HIST_SNAPSHOT_MENSAL = os.path.join(DATA_DIR, "snapshot_mensal.json")         # mm/aaaa -> estado (saldo/pix/domicilio/qualificadas)
HIST_VISAO_MENSAL = os.path.join(DATA_DIR, "visao_mensal_curada.json")        # mm/aaaa -> snapshot curado por cnpj
HIST_NOVA_PAGO_POR_CNPJ = os.path.join(DATA_DIR, "novo_pago_max_por_cnpj.json")
HIST_NOVA_RESUMO_MENSAL = os.path.join(DATA_DIR, "novo_resumo_mensal.json")
HIST_SUPERVISOR_C6_DAILY = os.path.join(DATA_DIR, "supervisor_c6_daily.json")
SUPERVISOR_C6_EMAIL_CFG = os.path.join(DATA_DIR, "supervisor_c6_email_config.json")

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
C6_DAILY_IMPORT_META = os.path.join(DATA_DIR, "c6_daily_import_meta.json")
C6_DAILY_FUNIL_TRACK = os.path.join(DATA_DIR, "c6_daily_funil_track.json")
C6_LEADS_CNPJ_TRACK = os.path.join(DATA_DIR, "c6_leads_cnpj_track.json")
C6_OPS_CACHE = os.path.join(DATA_DIR, "c6_operacao_ops_cache.bin")
C6_OPS_CACHE_META = os.path.join(DATA_DIR, "c6_operacao_ops_cache_meta.json")

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
def safe_json_load(path: str, default):
    """
    ✅ Se existir st.secrets["firebase"], lê do Firestore.
    Caso contrário, mantém comportamento local.
    """
    if "firebase" in st.secrets:
        return _fs_load_payload(_fs_doc_id_from_path(path), default)

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


def local_json_load(path: str, default):
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
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def local_json_delete(path: str):
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


def _normalize_person_key(value: str) -> str:
    txt = str(value or "").strip().upper()
    txt = unicodedata.normalize("NFKD", txt).encode("ASCII", "ignore").decode("ASCII")
    txt = re.sub(r"[_\-.]+", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


def render_downloadable_table(df_display, key_prefix: str, filename_prefix: str, raw_df=None, hide_index: bool = True, use_container_width: bool = True):
    st.dataframe(df_display, use_container_width=use_container_width, hide_index=hide_index)
    base = raw_df if raw_df is not None else getattr(df_display, "data", df_display)
    if isinstance(base, pd.Series):
        base = base.to_frame()
    if isinstance(base, pd.DataFrame):
        seq = st.session_state.get("_dl_btn_seq", 0)
        st.session_state["_dl_btn_seq"] = seq + 1
        unique_key = f"dl_{key_prefix}_{seq}"
        st.download_button(
            "⬇",
            data=_to_excel_bytes({"Tabela": base}),
            file_name=f"{filename_prefix}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=unique_key,
            use_container_width=False,
            help="Baixar Excel",
        )


def read_excel_any(file_bytes: bytes) -> pd.DataFrame:
    return pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")


def _save_daily_import_cache(kind: str, file_name: str, raw_bytes: bytes):
    kind_key = str(kind or "").strip().lower()
    if kind_key == "visao":
        cache_path = C6_DAILY_VISAO_CACHE
    elif kind_key == "lct":
        cache_path = C6_DAILY_LCT_CACHE
    else:
        cache_path = C6_DAILY_LEADS_CACHE
    with open(cache_path, "wb") as f:
        f.write(raw_bytes)
    meta = local_json_load(C6_DAILY_IMPORT_META, default={}) or {}
    meta[kind_key] = {
        "name": str(file_name or "").strip(),
        "cached_at": dt.datetime.now().isoformat(),
    }
    local_json_save(C6_DAILY_IMPORT_META, meta)


def _load_daily_import_cache(kind: str):
    kind_key = str(kind or "").strip().lower()
    if kind_key == "visao":
        cache_path = C6_DAILY_VISAO_CACHE
    elif kind_key == "lct":
        cache_path = C6_DAILY_LCT_CACHE
    else:
        cache_path = C6_DAILY_LEADS_CACHE
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
        return df, str(info.get("name") or ""), "Importação diária (cache local)"
    except Exception:
        return None, "", ""


def _save_ops_import_cache(file_name: str, raw_bytes: bytes):
    with open(C6_OPS_CACHE, "wb") as f:
        f.write(raw_bytes)
    local_json_save(C6_OPS_CACHE_META, {
        "name": str(file_name or "").strip(),
        "cached_at": dt.datetime.now().isoformat(),
    })


def _load_ops_import_cache():
    if not os.path.exists(C6_OPS_CACHE):
        return None, ""
    try:
        with open(C6_OPS_CACHE, "rb") as f:
            raw = f.read()
        df = _read_ops_file(type("CachedUpload", (), {"getvalue": lambda self: raw, "name": str((local_json_load(C6_OPS_CACHE_META, default={}) or {}).get("name") or "c6_operacao_cache.csv")})())
        meta = local_json_load(C6_OPS_CACHE_META, default={}) or {}
        return df, str(meta.get("name") or "")
    except Exception:
        return None, ""


def _truthy_flag(value) -> bool:
    txt = str(value or "").strip().upper()
    return txt in {"1", "S", "SIM", "TRUE", "ATIVA", "ATIVO", "YES"}


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

        wallet_raw = row.get("WALLET", row.get("FL_WALLET", row.get("CARTAO_WALLET", "")))
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
    return int(len(bdays))


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
    base[day_key] = payload
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
        "Qualificadas M0", "Δ Qualificadas M0",
        "Qualificadas M1", "Δ Qualificadas M1",
        "Qualificadas M2", "Δ Qualificadas M2",
        "Chaves Pix total", "Δ Chaves Pix total",
        "Saldo total (VL_CASH_IN_MTD)", "Δ Saldo total (VL_CASH_IN_MTD)",
        "Base (A receber no mês)", "Δ Base (A receber no mês)"
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
        "Qualificadas M0", "Delta mes anterior Qualificadas M0",
        "Qualificadas M1", "Delta mes anterior Qualificadas M1",
        "Qualificadas M2", "Delta mes anterior Qualificadas M2",
        "Chaves Pix total", "Delta mes anterior Chaves Pix total",
        saldo_col, f"Delta mes anterior {saldo_col}",
        base_col, f"Delta mes anterior {base_col}"
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


def _normalize_cnpj_text(v) -> str:
    return re.sub(r"\D", "", "" if v is None or pd.isna(v) else str(v))


def _nova_acc_factor(qtd_qualificadas: int) -> float:
    for min_q, factor in ACELERADORES_NOVA:
        if qtd_qualificadas >= min_q:
            return float(factor)
    return 1.0


def _nova_cashin_amount(faixa: int) -> float:
    mapa = {1: 250.0, 2: 400.0, 3: 600.0, 4: 750.0}
    return float(mapa.get(int(faixa or 0), 0.0))


def _nova_spending_amount(faixa: int, factor: float) -> float:
    base = {1: 500.0, 2: 800.0, 3: 1100.0, 4: 1400.0}
    return float(base.get(int(faixa or 0), 0.0)) * float(factor)


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
            "c6pay_ativa_30": int(pd.to_numeric(pd.Series([row.get("C6PAY_ATIVA_30")]), errors="coerce").fillna(0).iloc[0]),
            "dt_entrega_cartao": fmt_date(pd.to_datetime(row.get("DT_ENTREGA_CARTAO"), errors="coerce")),
            "dt_ativ_cartao_cred": fmt_date(pd.to_datetime(row.get("DT_ATIV_CARTAO_CRED"), errors="coerce")),
            "banco_domicilio": str(row.get(COL_DOMICILIO, "") or "").strip(),
            "wallet": str(row.get("WALLET", "") or "").strip().upper(),
            "data_base": fmt_date(pd.to_datetime(row.get(COL_DATA_BASE), errors="coerce")),
        })

    by_cnpj = {}
    for item in rows:
        by_cnpj[item["cnpj"]] = item
    store[mkey] = by_cnpj
    local_json_save(HIST_VISAO_MENSAL, store)


def _load_visao_month_snapshot() -> Dict[str, Dict[str, dict]]:
    return local_json_load(HIST_VISAO_MENSAL, default={}) or {}


def recompute_cartilha_nova() -> pd.DataFrame:
    visao_store = _load_visao_month_snapshot()
    months = sorted([m for m in visao_store.keys() if m in CARTILHA_NOVA_MESES], key=month_key_str)

    paid_max: Dict[str, float] = _old_paid_max_before("04/2026")
    resumo: Dict[str, dict] = {}
    rows = []

    for mkey in months:
        cmap = visao_store.get(mkey, {}) or {}
        valid_rows = []
        for cnpj, row in cmap.items():
            tipo = str(row.get("tipo_pessoa", "")).upper()
            status = str(row.get("status_cc", "")).upper()
            if tipo != "PJ":
                continue
            if "MEI" in tipo:
                continue
            if status in {"BLOQUEADA", "DESATIVADA", "ENCERRADA"}:
                continue
            valid_rows.append((cnpj, row))

        qtd_qual = 0
        current_amounts = {}
        detail_counts = {"cash_in": 0, "spending": 0, "c6pay": 0, "pix_cnpj": 0, "wallet": 0}

        tmp_amounts = {}
        for cnpj, row in valid_rows:
            fator = _nova_acc_factor(0)
            cash_amt = _nova_cashin_amount(int(row.get("faixa_cash_in", 0) or 0))
            spending_ok = "ATRAS" not in str(row.get("status_pagamento_fatura", "")).upper()
            spending_amt = _nova_spending_amount(int(row.get("faixa_spending", 0) or 0), fator) if spending_ok else 0.0
            tpv_amt = _nova_tpv_amount(_nova_tpv_for_stage(row), fator)
            best_amt = max(cash_amt, spending_amt, tpv_amt)
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

        fator_mes = _nova_acc_factor(qtd_qual)
        total_cheio = 0.0
        total_receber = 0.0

        for cnpj, item in tmp_amounts.items():
            row = item["row"]
            cash_amt = _nova_cashin_amount(int(row.get("faixa_cash_in", 0) or 0))
            spending_ok = "ATRAS" not in str(row.get("status_pagamento_fatura", "")).upper()
            spending_amt = _nova_spending_amount(int(row.get("faixa_spending", 0) or 0), fator_mes) if spending_ok else 0.0
            tpv_amt = _nova_tpv_amount(_nova_tpv_for_stage(row), fator_mes)
            best_amt = max(cash_amt, spending_amt, tpv_amt)

            if best_amt == tpv_amt and best_amt > 0:
                detail_counts["c6pay"] += 1
            elif best_amt == spending_amt and best_amt > 0:
                detail_counts["spending"] += 1
            elif best_amt == cash_amt and best_amt > 0:
                detail_counts["cash_in"] += 1

            prev = float(paid_max.get(cnpj, 0.0))
            diff = best_amt - prev
            if diff < 0:
                diff = 0.0

            total_cheio += best_amt
            total_receber += diff
            paid_max[cnpj] = max(prev, best_amt)
            current_amounts[cnpj] = best_amt

        if mkey == "06/2026":
            for cnpj, row in cmap.items():
                if cnpj not in current_amounts:
                    continue
                pix_raw = _pix_clean_value(row.get("chaves_pix_forte", ""))
                if _pix_has_cnpj(pix_raw):
                    total_cheio += 15.0
                    total_receber += 15.0
                    detail_counts["pix_cnpj"] += 1

        ja_pago_ref = total_cheio - total_receber
        resumo[mkey] = {
            "qualificadas": qtd_qual,
            "acelerador": fator_mes,
            "cash_in": detail_counts["cash_in"],
            "spending": detail_counts["spending"],
            "c6pay": detail_counts["c6pay"],
            "pix_cnpj": detail_counts["pix_cnpj"],
            "wallet": detail_counts["wallet"],
            "deveria_receber": total_cheio,
            "ja_pago_ref": ja_pago_ref,
            "receber_mes": total_receber,
        }
        rows.append([
            mkey, qtd_qual, fator_mes, detail_counts["cash_in"], detail_counts["spending"],
            detail_counts["c6pay"], detail_counts["pix_cnpj"], detail_counts["wallet"],
            total_cheio, ja_pago_ref, total_receber
        ])

    safe_json_save(HIST_NOVA_PAGO_POR_CNPJ, paid_max)
    safe_json_save(HIST_NOVA_RESUMO_MENSAL, resumo)

    return pd.DataFrame(
        rows,
        columns=[
            "Mês", "Qualificadas", "Acelerador", "Cash In", "Spending", "C6 Pay",
            "PIX CNPJ", "Wallet", "Deveria receber (cheio)", "Já pago (referência)", "A receber no mês"
        ],
    )


def compute_campanha_tri() -> pd.DataFrame:
    visao_store = _load_visao_month_snapshot()
    rows = []

    for mkey in ["04/2026", "05/2026", "06/2026"]:
        meta = CAMPANHA_2TRI_METAS.get(mkey, {})
        bucket_months = _campaign_bucket_months(mkey)
        bucket_rows = []
        current_rows = visao_store.get(mkey, {}) or {}

        for bm in bucket_months:
            for cnpj, row in (visao_store.get(bm, {}) or {}).items():
                tipo = str(row.get("tipo_pessoa", "")).upper()
                status = str(row.get("status_cc", "")).upper()
                if tipo != "PJ":
                    continue
                if "MEI" in tipo:
                    continue
                if status in {"BLOQUEADA", "DESATIVADA", "ENCERRADA"}:
                    continue
                bucket_rows.append((cnpj, row))

        bucket_by_cnpj = {}
        for cnpj, row in bucket_rows:
            bucket_by_cnpj[cnpj] = row

        start, end = _month_range(mkey)
        aberturas_mes = 0
        for cnpj, row in current_rows.items():
            tipo = str(row.get("tipo_pessoa", "")).upper()
            status = str(row.get("status_cc", "")).upper()
            if tipo != "PJ" or "MEI" in tipo or status in {"BLOQUEADA", "DESATIVADA", "ENCERRADA"}:
                continue
            try:
                d = dt.datetime.strptime(str(row.get("dt_conta_criada", "")), "%d/%m/%Y").date()
            except Exception:
                continue
            if start and end and start <= d <= end:
                aberturas_mes += 1

        qualificados_mes = 0
        ativ_pay_mes = 0
        for cnpj, row in bucket_by_cnpj.items():
            current = current_rows.get(cnpj)
            if not current:
                continue
            cash_amt = _nova_cashin_amount(int(current.get("faixa_cash_in", 0) or 0))
            spending_ok = "ATRAS" not in str(current.get("status_pagamento_fatura", "")).upper()
            spending_amt = _nova_spending_amount(int(current.get("faixa_spending", 0) or 0), 1.0) if spending_ok else 0.0
            tpv_amt = _nova_tpv_amount(_nova_tpv_for_stage(current), 1.0)
            if max(cash_amt, spending_amt, tpv_amt) > 0:
                qualificados_mes += 1
            if float(_nova_tpv_for_stage(current)) >= 1000.0:
                ativ_pay_mes += 1

        bucket_total = len(bucket_by_cnpj)
        perc_qual = (qualificados_mes / bucket_total) if bucket_total > 0 else 0.0
        bateu_percentual = perc_qual >= float(CAMPANHA_2TRI_METAS["TRI"]["perc_min"])
        bateu_mensal = (
            aberturas_mes >= int(meta.get("abertura", 0))
            and qualificados_mes >= int(meta.get("qualificacao", 0))
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
        "% Qualificação": (float(df["Qualificados"].sum()) / float(max(df["Balde válido"].sum(), 1))),
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
    cash_amt = _nova_cashin_amount(int(row.get("faixa_cash_in", 0) or 0))
    spending_ok = "ATRAS" not in str(row.get("status_pagamento_fatura", "")).upper()
    spending_amt = _nova_spending_amount(int(row.get("faixa_spending", 0) or 0), 1.0) if spending_ok else 0.0
    tpv_amt = _nova_tpv_amount(_nova_tpv_for_stage(row), 1.0)
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
            dt_pay = _parse_br_date_text(row.get("dt_ativacao_pay"))
            dt_entrega = _parse_br_date_text(row.get("dt_entrega_cartao"))
            dt_cartao = _parse_br_date_text(row.get("dt_ativ_cartao_cred"))
            mes_ref = str(row.get("mes_ref_comiss", "") or "").strip().upper()
            pix_raw = _pix_clean_value(row.get("chaves_pix_forte", ""))
            wallet_raw = str(row.get("wallet", "") or "").upper()
            flags = _supervisor_cartilha_flags(row)
            level = _supervisor_level(row)

            if start and end and dt_abertura and start <= dt_abertura <= end:
                abertas_mes.add(cnpj)
                q_open.add(cnpj)
            if start and end and dt_install and start <= dt_install <= end:
                install_mes.add(cnpj)
                q_install.add(cnpj)
            if start and end and dt_pay and start <= dt_pay <= end:
                ativ_pay_mes.add(cnpj)
                q_pay.add(cnpj)
            if flags["qualificado"]:
                qual_mes.add(cnpj)
                q_qual.add(cnpj)
            if flags["tpv"] and contains_c6(row.get("banco_domicilio", "")):
                dom_mes.add(cnpj)
                q_dom.add(cnpj)
            if flags["spending"]:
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
def _criterio_score(txt: str, nome: str) -> int:
    if not isinstance(txt, str) or not txt.strip():
        return 0
    m = re.search(rf"{re.escape(nome)}\s*:\s*(\d+)", txt, flags=re.IGNORECASE)
    return int(m.group(1)) if m else 0


def _supervisor_snapshot_from_records(rows: List[dict], report_day: Optional[dt.date] = None) -> dict:
    start = dt.date(2026, 4, 1)
    end = dt.date(2026, 6, 30)
    month_start = month_first(report_day) if report_day else None
    if month_start:
        if month_start.month == 12:
            month_end = dt.date(month_start.year + 1, 1, 1) - dt.timedelta(days=1)
        else:
            month_end = dt.date(month_start.year, month_start.month + 1, 1) - dt.timedelta(days=1)
    else:
        month_end = None

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
    nivel4 = 0

    for row in rows:
        dt_abertura = _parse_br_date_text(row.get("dt_conta_criada"))
        dt_install = _parse_br_date_text(row.get("dt_install_maq"))
        dt_pay = _parse_br_date_text(row.get("dt_ativacao_pay"))
        dt_entrega = _parse_br_date_text(row.get("dt_entrega_cartao"))
        dt_cartao = _parse_br_date_text(row.get("dt_ativ_cartao_cred"))
        mes_ref = str(row.get("mes_ref_comiss", "") or "").strip().upper()
        pix_raw = _pix_clean_value(row.get("chaves_pix_forte", ""))
        criterios = str(row.get("criterios_atingidos_comiss", "") or "")
        level = _supervisor_level(row)

        if dt_abertura and start <= dt_abertura <= end:
            contas_abertas += 1
        if _supervisor_cartilha_flags(row)["qualificado"]:
            contas_qualificadas += 1
        if dt_install and month_start and month_end and month_start <= dt_install <= month_end:
            instalacoes_c6pay += 1
        if dt_pay and month_start and month_end and month_start <= dt_pay <= month_end:
            c6pay_ativadas += 1
        if mes_ref in {"M0", "M1", "M2"}:
            pix_base += 1
            if _pix_has_cnpj(pix_raw):
                pix_cnpj += 1
        if _criterio_score(criterios, "DOMICILIO") > 0:
            domicilio_qualificado += 1
        if _criterio_score(criterios, "SPENDING") > 0:
            spending_qualificado += 1
        if dt_entrega:
            cartoes_entregues += 1
        if dt_cartao:
            cartoes_ativados += 1
        if level >= 4:
            nivel4 += 1

    pix_pct = (pix_cnpj / pix_base) if pix_base > 0 else 0.0
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
            "Realizado_num": 0.0,
            "Meta_num": float(SUPERVISOR_C6_METAS["wallet"]["meta"]),
            "Atingimento_num": 0.0,
            "Premio_num": float(SUPERVISOR_C6_METAS["wallet"]["premio"]),
            "Recebe_num": 0.0,
            "Faixa": "",
            "Status": "Sem base",
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
    return {
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
        "ativacao_cartao_pct": ativacao_cartao_pct,
        "nivel4": nivel4,
        "indicadores": indicadores,
    }


def persist_supervisor_c6_daily(df_c6: pd.DataFrame):
    report_day = detect_report_day_from_df(df_c6)
    if report_day is None or COL_CNPJ not in df_c6.columns:
        return

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
    store = safe_json_load(HIST_SUPERVISOR_C6_DAILY, default={}) or {}
    store[fmt_date(report_day)] = snapshot
    safe_json_save(HIST_SUPERVISOR_C6_DAILY, store)


def _format_supervisor_indicator_view(df_supervisor: pd.DataFrame) -> pd.DataFrame:
    view = df_supervisor.copy()
    def _premio_display(row):
        indicador = str(row.get("Indicador", "")).strip()
        if indicador == "Contas qualificadas":
            q_real = int(float(row.get("Realizado_num", 0) or 0))
            premio_ref = 400.0
            if q_real >= 1000:
                premio_ref = 1400.0
            elif q_real >= 900:
                premio_ref = 540.0
            elif q_real >= 800:
                premio_ref = 500.0
            return br_money(premio_ref)
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
        indicador = str(row.get("Indicador", "")).strip()
        if indicador == "Contas qualificadas":
            q_real = int(float(row.get("Realizado_num", 0) or 0))
            premio_ref = 400.0
            if q_real >= 1000:
                premio_ref = 1400.0
            elif q_real >= 900:
                premio_ref = 540.0
            elif q_real >= 800:
                premio_ref = 500.0
            return br_money(premio_ref)
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
            f"Contas abertas: <b>{br_int(int(summary.get('contas_abertas', 0)))}</b> de <b>{br_int(int(SUPERVISOR_C6_METAS['contas_abertas']['meta']))}</b>",
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
        maintype = str(att.get("maintype") or "application")
        subtype = str(att.get("subtype") or "octet-stream")
        filename = str(att.get("filename") or "anexo.bin")
        data = att.get("data") or b""
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)

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


def compute_supervisor_c6_meta() -> Tuple[pd.DataFrame, pd.DataFrame, dict]:
    store = safe_json_load(HIST_SUPERVISOR_C6_DAILY, default={}) or {}
    if "c6_daily_visao_df" in st.session_state:
        try:
            persist_supervisor_c6_daily(st.session_state["c6_daily_visao_df"])
            store = safe_json_load(HIST_SUPERVISOR_C6_DAILY, default={}) or {}
        except Exception:
            if not store:
                store = {}
    if not store:
        return pd.DataFrame(), pd.DataFrame(), {}

    latest_day = sorted(store.keys(), key=lambda x: dt.datetime.strptime(x, "%d/%m/%Y"))[-1]
    snapshot = store.get(latest_day) or {}
    indicators = pd.DataFrame(snapshot.get("indicadores", []))
    if not indicators.empty and "Indicador" in indicators.columns:
        mask = indicators["Indicador"].astype(str) == "Contas qualificadas"
        if mask.any():
            q_real = int(snapshot.get("contas_qualificadas", 0))
            premio_ref = 400.0
            if q_real >= 1000:
                premio_ref = 1400.0
            elif q_real >= 900:
                premio_ref = 540.0
            elif q_real >= 800:
                premio_ref = 500.0
            indicators.loc[mask, "Premio_num"] = float(premio_ref)
    daily_df = _supervisor_daily_evolution_df(store)
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
                --am-ink: #10233f;
                --am-ink-soft: #53657f;
                --am-navy: #17304f;
                --am-blue: #2b5aa6;
                --am-bg: #f7f9fc;
                --am-panel: #ffffff;
                --am-line: #dde5ef;
                --am-positive: #117a43;
                --am-positive-bg: #e7f6ee;
                --am-negative: #b42318;
                --am-negative-bg: #fdecec;
                --am-shadow: 0 8px 20px rgba(16,35,63,0.05);
            }
            .stApp {
                background: linear-gradient(180deg, #fbfcfe 0%, var(--am-bg) 100%);
                color: var(--am-ink);
            }
            .block-container {
                max-width: 1380px;
                padding-top: 1rem;
                padding-bottom: 3rem;
            }
            section[data-testid="stSidebar"]{
                background: linear-gradient(180deg, #10233f 0%, #153052 100%);
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
                border-radius:16px;
                padding:14px 16px;
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
                letter-spacing: -0.02em;
                font-size: clamp(1.55rem, 1.7vw, 2.15rem) !important;
                line-height: 1.05 !important;
            }
            h1, h2, h3{
                color: var(--am-ink);
                font-weight: 700;
                letter-spacing: -0.02em;
            }
            p, label, .stCaption {
                color: var(--am-ink-soft);
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
                border-radius: 14px;
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
                border-radius: 14px;
                padding: 20px;
                background: #fcfdff;
            }
            .stTabs [data-baseweb="tab-list"] {
                gap: 8px;
                background: rgba(255,255,255,0.92);
                padding: 8px;
                border-radius: 14px;
                border: 1px solid var(--am-line);
            }
            .stTabs [data-baseweb="tab"] {
                height: 44px;
                padding: 0 16px;
                border-radius: 10px;
                color: var(--am-ink-soft);
                font-weight: 650;
            }
            .stTabs [aria-selected="true"] {
                background: #eaf1fb !important;
                border: 1px solid #c9d7ea !important;
                box-shadow: none;
                color: var(--am-ink) !important;
            }
            .stTabs [aria-selected="true"] p {
                color: var(--am-ink) !important;
            }
            .stButton button, .stDownloadButton button {
                border-radius: 12px !important;
                border: 1px solid var(--am-line) !important;
                font-weight: 700 !important;
                min-height: 42px;
                box-shadow: none;
                background: white !important;
                color: var(--am-ink) !important;
            }
            .stButton button[kind="primary"], .stDownloadButton button[kind="primary"] {
                background: var(--am-navy) !important;
                color: white !important;
                border: none !important;
            }
            .stTextInput input, .stSelectbox [data-baseweb="select"] > div, .stTextArea textarea {
                border-radius: 14px !important;
                border: 1px solid var(--am-line) !important;
                background: rgba(255,255,255,0.98) !important;
            }
            div[data-testid="stAlert"] {
                border-radius: 18px;
                border: 1px solid var(--am-line);
            }
            div[data-testid="stExpander"] {
                border: 1px solid var(--am-line);
                border-radius: 18px;
                overflow: hidden;
                background: linear-gradient(180deg, rgba(255,255,255,0.97), rgba(247,250,255,0.97));
            }
            div[data-testid="stExpander"] summary {
                font-weight: 800;
                color: var(--am-ink);
            }
            .am-hero-box {
                border-radius: 22px;
                padding: 24px 26px;
                background: linear-gradient(135deg, #10233f 0%, #17304f 100%);
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
                letter-spacing: -0.04em;
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
    here = os.getcwd()
    logo_path = os.path.join(here, "LOGO CORRETA.png")

    c1, c2 = st.columns([1.1, 5.9], vertical_alignment="center")
    with c1:
        if os.path.exists(logo_path):
            st.image(logo_path, width=150)
        else:
            st.warning("Logo não encontrada. Coloque 'LOGO CORRETA.png' na raiz do projeto.")
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
        META_SUMMARY_PATH,
        META_GROUPS_PATH,
        META_FILE_CONTROL,
        META_GROUPS_CONTROL,
    ]:
        safe_json_delete(p)
    local_json_delete(HIST_VISAO_MENSAL)


# =========================================================
# C6 OPERAÇÃO — NOVO MÓDULO
# =========================================================
C6_OP_IMPORT_LOG = os.path.join(DATA_DIR, "c6_op_importacoes.json")
C6_OP_PIX_TRACK = os.path.join(DATA_DIR, "c6_op_pix_track.json")
C6_OP_OMC_MAXPAY = os.path.join(DATA_DIR, "c6_op_omc_maxpay.json")

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
                    return pd.DataFrame(rows, columns=fixed_cols, dtype=str)
        except Exception:
            pass
        last_err = None
        for enc in ["utf-8-sig", "latin1", "cp1252"]:
            for sep in [";", ",", "\t", "|"]:
                try:
                    df = pd.read_csv(io.BytesIO(raw), sep=sep, dtype=str, encoding=enc)
                    if len(df.columns) > 1:
                        return df
                except Exception as e:
                    last_err = e
        raise last_err
    return pd.read_excel(io.BytesIO(raw), dtype=str, engine="openpyxl")

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
    data_base_col = _coalesce_col(df, ["DATA_BASE"])
    cadastro_col = _coalesce_col(df, ["DATA_HORA_CADASTRO", "DATA_CADASTRO"])
    aberta_col = _coalesce_col(df, ["DT_CONTA_ABERTA", "DT_CONTA_CRIADA"])
    status_abertura_col = _coalesce_col(df, ["STATUS_ABERTURA_CONTA"])
    status_final_col = _coalesce_col(df, ["STATUS_FINAL"])
    pendencias_col = _coalesce_col(df, ["PENDENCIAS"])

    out = pd.DataFrame()
    out["cnpj"] = _normalize_cnpj_series(df[cnpj_col]) if cnpj_col else ""
    out["nome_cliente"] = normalize_str(df[nome_col]) if nome_col else ""
    out["data_base"] = pd.to_datetime(df[data_base_col], errors="coerce", dayfirst=True) if data_base_col else pd.NaT
    out["data_hora_cadastro"] = pd.to_datetime(df[cadastro_col], errors="coerce", dayfirst=True) if cadastro_col else pd.NaT
    out["dt_conta_aberta_leads"] = pd.to_datetime(df[aberta_col], errors="coerce", dayfirst=True) if aberta_col else pd.NaT
    out["status_abertura_conta"] = normalize_str(df[status_abertura_col]) if status_abertura_col else ""
    out["status_final"] = normalize_str(df[status_final_col]) if status_final_col else ""
    out["pendencias"] = normalize_str(df[pendencias_col]) if pendencias_col else ""
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
    pix_col = _coalesce_col(df, ["CHAVES_PIX_FORTE"])
    cashin_col = _coalesce_col(df, ["VL_CASH_IN_MTD"])
    wallet_col = _coalesce_col(df, ["WALLET", "FL_WALLET", "CARTAO_WALLET"])
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


def _read_lct_file_any(name: str, raw_bytes: bytes) -> Optional[pd.DataFrame]:
    try:
        if str(name or "").lower().endswith(".csv"):
            sample = raw_bytes[:200_000].decode("utf-8-sig", errors="replace")
            sep = _detect_delim_for_csv(sample) if "_detect_delim_for_csv" in globals() else ","
            return pd.read_csv(io.BytesIO(raw_bytes), sep=sep, engine="python", on_bad_lines="skip", encoding="utf-8-sig")
        return pd.read_excel(io.BytesIO(raw_bytes))
    except Exception as e:
        st.error(f"Erro ao ler Resumo LCT: {e}")
        return None


def _extract_lct_base(df_lct: pd.DataFrame) -> pd.DataFrame:
    df = df_lct.copy()
    nome_col = _coalesce_col(df, ["Nome", "NOME", "NOME_CLIENTE"])
    cnpj_col = _coalesce_col(df, ["CPF / CNPJ", "CNPJ", "CNPJ_CLIENTE"])
    data_col = _coalesce_col(df, ["Data", "DATA"])
    fase_col = _coalesce_col(df, ["Fase", "FASE"])
    acao_col = _coalesce_col(df, ["Ação", "ACAO", "Acao"])

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
    out = out[out["cnpj"] != ""].copy()
    if "acao_lct" in out.columns and out["acao_lct"].astype(str).str.strip().ne("").any():
        out = out[out["acao_lct"].astype(str).str.contains("LCT", na=False)].copy()
    out["data_lct_dia"] = pd.to_datetime(out["data_lct"], errors="coerce").dt.date
    if out["data_lct_dia"].isna().all() and data_col:
        out["data_lct_dia"] = pd.to_datetime(df.loc[out.index, data_col], errors="coerce", dayfirst=True).dt.date
    out = out.sort_values(["data_lct", "cnpj"]).drop_duplicates(subset=["cnpj", "data_lct_dia"], keep="last")
    return out

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

def _process_c6_operacao(df_ops_raw: pd.DataFrame, df_leads_raw: pd.DataFrame, df_visao_raw: pd.DataFrame, persist_history: bool = False) -> Dict[str, pd.DataFrame]:
    hist = _load_c6_ops_history()
    imports_log = hist["imports"] if isinstance(hist["imports"], list) else []
    pix_track = hist["pix_track"] if isinstance(hist["pix_track"], dict) else {}
    omc_maxpay = hist["omc_maxpay"] if isinstance(hist["omc_maxpay"], dict) else {}

    ops = _extract_ops_base(df_ops_raw)
    leads = _extract_leads_base(df_leads_raw)
    visao = _extract_visao_base(df_visao_raw)

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
    act_rep["valor_indicacao"] = act_rep["operador"].fillna("").apply(lambda x: 0.25 if str(x).strip() else 0.0)
    act_rep["valor_bonus"] = act_rep["janela_14d"].apply(lambda x: 2.0 if x else 0.0)
    act_rep["valor_total"] = act_rep["valor_indicacao"] + act_rep["valor_bonus"]
    act_rep["faixa_idade_empresa"] = act_rep.apply(lambda r: _faixa_idade_empresa(r.get("dt_fundacao_empresa"), r.get("data_base")), axis=1)
    act_rep["dias_ate_abertura"] = act_rep.apply(lambda r: _days_between(r.get("data_hora_cadastro"), r.get("dt_conta_criada")), axis=1)
    act_rep["abriu_apos_indicacao"] = act_rep.apply(
        lambda r: pd.notna(r.get("dt_conta_criada")) and pd.notna(r.get("data_acao")) and pd.Timestamp(r.get("dt_conta_criada")).normalize() >= pd.Timestamp(r.get("data_acao")).normalize(),
        axis=1,
    )
    act_rep["mes_ref"] = act_rep["data_hora_cadastro"].dt.strftime("%Y-%m").fillna("")
    act_rep = act_rep[act_rep["operador"].fillna("").astype(str).str.strip().ne("")].copy()
    act_rep = act_rep[["operador", "nome_cliente", "cnpj", "data_acao", "data_hora_cadastro", "dt_conta_criada", "dt_fundacao_empresa", "faixa_idade_empresa", "dias_ate_abertura", "abriu_apos_indicacao", "janela_14d", "valor_indicacao", "valor_bonus", "valor_total", "mes_ref"]].sort_values(["operador", "data_acao", "nome_cliente"], na_position="last")
    act_oper = act_rep.groupby("operador", dropna=False).agg(clientes_indicados=("cnpj", "nunique"), contas_abertas=("abriu_apos_indicacao", "sum"), abertas_14d=("janela_14d", "sum"), comissao_total=("valor_total", "sum")).reset_index()
    act_oper["eficiencia_%"] = (act_oper["contas_abertas"] / act_oper["clientes_indicados"].replace(0, pd.NA) * 100).fillna(0).round(2)
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
    valid_df = oab_rep[oab_rep["abriu_apos_acao"]].sort_values(["operador", "dt_conta_criada"])
    for (operador, mes_ref), grp in valid_df.groupby(["operador", "mes_ref"]):
        for pos, row_idx in enumerate(grp.index.tolist(), start=1):
            oab_rep.at[row_idx, "valor_unitario"] = 5.0 if pos >= 151 else 3.0
    oab_rep["valor_total"] = oab_rep["valor_unitario"]
    bko_all = oab_rep[oab_rep["status_abertura_conta"].apply(_normalize_status_key).eq("AGUARDAR ATUACAO MANUAL BKO")].copy()
    if not bko_all.empty:
        def _bucket_bko(x):
            if x is None: return ""
            if x <= 1: return "1 dia útil"
            if x == 2: return "2 dias úteis"
            if x == 3: return "3 dias úteis"
            if x == 4: return "4 dias úteis"
            return "5+ dias úteis"
        bko_all["bucket_bko"] = bko_all["dias_uteis_bko"].apply(_bucket_bko)
        bko_alerta = bko_all[bko_all["dias_uteis_bko"].fillna(0) >= 5].copy()
        bko_alerta = bko_alerta[[
            "nome_cliente", "cnpj", "operador", "data_acao", "data_base", "data_hora_cadastro",
            "status_abertura_conta", "dias_uteis_bko", "bucket_bko", "pendencias", "dt_conta_criada"
        ]].sort_values(["dias_uteis_bko", "nome_cliente"], ascending=[False, True], na_position="last")
        bko_sum = bko_all.groupby("bucket_bko").size().reset_index(name="quantidade").rename(columns={"bucket_bko": "faixa"})
        bko_sum = bko_sum.set_index("faixa").reindex(["1 dia útil","2 dias úteis","3 dias úteis","4 dias úteis","5+ dias úteis"], fill_value=0).reset_index()
    else:
        bko_alerta = pd.DataFrame(columns=["nome_cliente", "cnpj", "operador", "data_acao", "data_base", "data_hora_cadastro", "status_abertura_conta", "dias_uteis_bko", "bucket_bko", "pendencias", "dt_conta_criada"])
        bko_sum = pd.DataFrame({"faixa": ["1 dia útil","2 dias úteis","3 dias úteis","4 dias úteis","5+ dias úteis"], "quantidade": [0,0,0,0,0]})
    oab_rep = oab_rep[oab_rep["operador"].fillna("").astype(str).str.strip().ne("")].copy()
    oab_screen = oab_rep[["operador", "nome_cliente", "cnpj", "data_acao", "data_hora_cadastro", "dt_conta_criada", "dias_ate_abertura", "faixa_abertura", "status_abertura_conta", "dias_uteis_bko", "abriu_apos_acao", "valor_unitario", "valor_total", "mes_ref"]].sort_values(["operador", "data_acao", "dt_conta_criada"], na_position="last")
    oab_oper = oab_rep.groupby("operador", dropna=False).agg(clientes_trabalhados=("cnpj", "nunique"), contas_validas=("abriu_apos_acao", "sum"), comissao_total=("valor_total", "sum")).reset_index()
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
    omc_rep["valor_ja_pago"] = omc_rep["cnpj"].apply(lambda c: float((omc_maxpay.get(c) or {}).get("max_paid", 0.0)))
    omc_rep["valor_teorico"] = pd.to_numeric(omc_rep["valor_teorico"], errors="coerce").fillna(0.0)
    omc_rep["valor_ja_pago"] = pd.to_numeric(omc_rep["valor_ja_pago"], errors="coerce").fillna(0.0)
    omc_rep["mes_ja_pago"] = omc_rep["cnpj"].apply(lambda c: (omc_maxpay.get(c) or {}).get("month", ""))
    omc_rep["valor_real_agora"] = (omc_rep["valor_teorico"] - omc_rep["valor_ja_pago"]).clip(lower=0)
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
    omc_screen = omc_screen[["operador", "nome_cliente", "cnpj", "data_acao", "data_base", "dt_conta_criada", "estagio_m", "criterios_atingidos_comiss", "nivel_maximo", "qualificado_valido", "valor_teorico", "valor_ja_pago", "mes_ja_pago", "valor_real_agora", "pix", "pix_tipo", "pix_primeira_aparicao", "pix_operador_origem", "pix_retirado_em", "wallet", "fl_propensao_c6pay", "tpv_c6pay_potencial", "fl_elegivel_venda_c6pay", "status_proposta_sf_pay", "dt_aprovacao_pay", "dt_install_maq", "dt_ativacao_pay", "c6pay_ativa_30", "dt_ult_trans_pay", "vl_cash_in_mtd", "banco_domicilio", "acao_valida_mes"]].sort_values(["operador", "valor_real_agora", "nome_cliente"], ascending=[True, False, True], na_position="last")
    omc_oper = omc_screen.groupby("operador", dropna=False).agg(clientes_base=("cnpj", "nunique"), qualificados=("qualificado_valido", "sum"), nivel4=("nivel_maximo", lambda s: int((pd.to_numeric(s, errors="coerce").fillna(0) >= 4).sum())), valor_teorico_total=("valor_teorico", "sum"), valor_real_total=("valor_real_agora", "sum")).reset_index()
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

    return {"act_report": act_rep, "act_operadores": act_oper.sort_values(["comissao_total", "abertas_14d"], ascending=False), "act_faixa": act_faixa, "oab_report": oab_screen, "oab_operadores": oab_oper.sort_values(["comissao_total", "contas_validas"], ascending=False), "bko_alerta": bko_alerta.sort_values(["dias_uteis_bko", "nome_cliente"], ascending=[False, True]) if not bko_alerta.empty else bko_alerta, "bko_summary": bko_sum, "omc_report": omc_screen, "omc_operadores": omc_oper.sort_values(["valor_real_total", "qualificados"], ascending=False), "resumo": resumo}

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
        "dias_ate_abertura", "dias_uteis_bko", "qtd_qual_op_mes", "quantidade"
    ]
    money_like = [
        "comissao_total", "valor_indicacao", "valor_bonus", "valor_total",
        "valor_unitario", "valor_teorico", "valor_ja_pago", "valor_real_agora",
        "valor_teorico_total", "valor_real_total"
    ]
    pct_like = ["eficiencia_%", "taxa_abertura_%", "eficiencia_vs_indicados_%", "eficiencia_qualificacao_%"]

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
    for k in ["act_report", "act_operadores", "oab_report", "oab_operadores", "omc_report", "omc_operadores", "bko_alerta"]:
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


def _render_c6_operacao_tab(view_only: bool = False, operator_filter: str = ""):
    st.subheader("🏦 C6 Operação")
    st.caption("Módulo operacional de Indicação (ACT), Conta Aberta (OCO) e Qualificação (OQL), com análises em tela e exportação Excel.")
    st.markdown("""
        <div style="padding:18px 20px;border-radius:18px;background:linear-gradient(135deg,#0f1b3a 0%,#1d4ed8 100%);color:white;margin-bottom:14px;">
            <div style="font-size:24px;font-weight:800;">C6 Operação</div>
            <div style="font-size:14px;opacity:0.92;">
                Acompanhe resultados por operador, abertura no prazo, aging de BKO, eficiência M0/M1/M2,
                Pix, Wallet, C6 Pay e comissão incremental dos qualificadores.
            </div>
        </div>
        """, unsafe_allow_html=True)
    up_ops = None
    if not view_only:
        u1 = st.columns(1)[0]
        with u1:
            up_ops = st.file_uploader("XPrisma / Grelacd (.csv ou .xlsx)", type=["csv", "xlsx"], key="c6_operacao_ops")
    up_leads = None
    up_visao = None
    if view_only:
        st.caption("Modo visualização. Esta tela usa automaticamente os últimos arquivos processados.")
    else:
        st.caption("Leads e Visao Cliente sao reaproveitados da Importacao diaria do Painel C6 Empresas. Nesta aba, envie apenas o XPrisma / Grelacd.")
    if up_ops:
        raw_ops_bytes = up_ops.getvalue()
        st.session_state["c6_operacao_ops_df"] = _read_ops_file(up_ops)
        st.session_state["c6_operacao_ops_df__name"] = up_ops.name
        st.session_state["c6_operacao_ops_df__ts"] = dt.datetime.now().timestamp()
        _save_ops_import_cache(up_ops.name, raw_ops_bytes)

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
    if df_ops_raw is None:
        df_ops_raw, ops_name = _load_ops_import_cache()

    if leads_name:
        st.caption(f"Leads em uso: {leads_origin} - {leads_name}")
    if visao_name:
        st.caption(f"Visao Cliente em uso: {visao_origin} - {visao_name}")
    if ops_name:
        st.caption(f"Arquivo operacional em uso: {ops_name}")

    if not view_only:
        st.markdown("""
            <div style="margin:10px 0 8px 0;padding:10px 14px;border-radius:14px;background:#eef4ff;border:1px solid #cfe0ff;color:#173858;font-size:13px;font-weight:600;">
                Importe o <b>XPrisma / Grelacd</b> e clique abaixo para cruzar com os últimos arquivos de <b>Leads</b> e <b>Visão Cliente</b> já enviados no Painel C6 Empresas.
            </div>
        """, unsafe_allow_html=True)
    processar = False if view_only else st.button("🚀 Processar agora a C6 Operação", use_container_width=True, type="primary")
    gravar = processar
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
    if view_only:
        result = st.session_state.get("c6_operacao_last_result")
        if not result:
            try:
                result = _process_c6_operacao(df_ops_raw, df_leads_raw, df_visao_raw, persist_history=False)
            except Exception as e:
                st.exception(e)
                return
    elif not (processar or gravar):
        st.caption("Os dados abaixo usam automaticamente os últimos arquivos enviados.")
        result = st.session_state.get("c6_operacao_last_result")
        if not result:
            try:
                result = _process_c6_operacao(df_ops_raw, df_leads_raw, df_visao_raw, persist_history=False)
            except Exception as e:
                st.exception(e)
                return
    else:
        try:
            result = _process_c6_operacao(df_ops_raw, df_leads_raw, df_visao_raw, persist_history=gravar)
        except Exception as e:
            st.exception(e)
            return
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
        render_downloadable_table(_format_report_df(result["bko_alerta"]), "c6_bko_top", "bko_5mais_dias", raw_df=result["bko_alerta"])
    sec1, sec2, sec3 = st.tabs(["ACT · Indicadores", "OCO · Abertura", "OQL · Qualificadores"])
    with sec1:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Clientes indicados", br_int(int(result["act_report"]["cnpj"].nunique())))
        c2.metric("Contas abertas na base", br_int(int(result["act_report"]["dt_conta_criada"].notna().sum())))
        c3.metric("Abertas após indicação ACT", br_int(int(result["act_report"]["abriu_apos_indicacao"].sum())))
        c4.metric("Comissão ACT", br_money(float(result["act_report"]["valor_total"].sum())))
        st.caption("Contas abertas na base = clientes do Leads que já aparecem com conta aberta no Visão Cliente. Abertas após indicação ACT = apenas as que valem para a análise do operador ACT.")
        st.markdown("#### Ranking dos Indicadores")
        render_downloadable_table(_format_report_df(result["act_operadores"]), "c6_act_oper", "c6_act_operadores", raw_df=result["act_operadores"])
        if not operator_filter:
            st.markdown("#### Qual perfil abre mais conta?")
            render_downloadable_table(_format_report_df(result["act_faixa"]), "c6_act_faixa", "c6_act_faixa", raw_df=result["act_faixa"])
        st.markdown("#### Analítico ACT")
        render_downloadable_table(_format_report_df(result["act_report"]), "c6_act_report", "c6_act_analitico", raw_df=result["act_report"])
    with sec2:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Clientes trabalhados", br_int(int(result["oab_report"]["cnpj"].nunique())))
        c2.metric("Contas válidas p/ comissão", br_int(int(result["oab_report"]["abriu_apos_acao"].sum())))
        med = result["oab_report"].loc[result["oab_report"]["abriu_apos_acao"] == True, "dias_ate_abertura"].dropna()
        c3.metric("Tempo médio até abertura", f"{med.mean():.1f} dias".replace(".", ",") if len(med) else "0,0 dia")
        c4.metric("Comissão OCO", br_money(float(result["oab_report"]["valor_total"].sum())))
        st.caption("O analítico OCO abaixo mostra toda a base trabalhada pelo operador. A coluna `abriu_apos_acao` indica quais clientes realmente geram comissão.")
        st.markdown("#### Ranking de abertura")
        render_downloadable_table(_format_report_df(result["oab_operadores"]), "c6_oco_oper", "c6_oco_operadores", raw_df=result["oab_operadores"])
        if not operator_filter:
            st.markdown("#### Aging BKO para sinalização ao banco")
            render_downloadable_table(_format_report_df(result["bko_summary"]), "c6_bko_summary", "c6_bko_summary", raw_df=result["bko_summary"])
        st.markdown("#### Analítico OCO")
        render_downloadable_table(_format_report_df(result["oab_report"]), "c6_oco_report", "c6_oco_analitico", raw_df=result["oab_report"])
    with sec3:
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Clientes base", br_int(int(result["omc_report"]["cnpj"].nunique())))
        c2.metric("Qualificados válidos", br_int(int(result["omc_report"]["qualificado_valido"].sum())))
        c3.metric("Nível 4", br_int(int((result["omc_report"]["nivel_maximo"] >= 4).sum())))
        c4.metric("Valor teórico", br_money(float(result["omc_report"]["valor_teorico"].sum())))
        c5.metric("Valor real agora", br_money(float(result["omc_report"]["valor_real_agora"].sum())))
        st.markdown("#### Ranking dos qualificadores")
        render_downloadable_table(_format_report_df(result["omc_operadores"]), "c6_oql_oper", "c6_oql_operadores", raw_df=result["omc_operadores"])
        st.markdown("#### Análise M0 / M1 / M2")
        estagio = result["omc_report"][result["omc_report"]["qualificado_valido"]].groupby("estagio_m")["cnpj"].nunique().reset_index(name="clientes")
        if not estagio.empty:
            total_est = estagio["clientes"].sum()
            estagio["eficiencia_%"] = (estagio["clientes"] / total_est * 100).round(2)
        render_downloadable_table(_format_report_df(estagio), "c6_oql_estagio", "c6_oql_estagio", raw_df=estagio)
        st.markdown("#### Pix, Wallet e C6 Pay")
        pix_sum = pd.DataFrame([
            {"Indicador": "Com Pix", "Quantidade": int(result["omc_report"]["pix"].astype(str).str.strip().ne("").sum())},
            {"Indicador": "Sem Pix", "Quantidade": int(result["omc_report"]["pix"].astype(str).str.strip().eq("").sum())},
            {"Indicador": "Pix CNPJ", "Quantidade": int(result["omc_report"]["pix"].apply(_pix_has_cnpj).sum())},
            {"Indicador": "Com Wallet", "Quantidade": int(result["omc_report"]["wallet"].astype(str).str.upper().isin(["1","SIM","TRUE","S"]).sum())},
            {"Indicador": "C6 Pay ativa 30", "Quantidade": int(result["omc_report"]["c6pay_ativa_30"].astype(str).str.upper().isin(["1","SIM","TRUE","S"]).sum())},
        ])
        render_downloadable_table(_format_report_df(pix_sum), "c6_oql_pixsum", "c6_oql_pix_wallet_pay", raw_df=pix_sum)
        st.markdown("#### Analítico OQL")
        render_downloadable_table(_format_report_df(result["omc_report"]), "c6_oql_report", "c6_oql_analitico", raw_df=result["omc_report"])
    excel_bytes = _to_excel_bytes({"Resumo": result["resumo"], "ACT_Operadores": result["act_operadores"], "ACT_Analitico": result["act_report"], "OCO_Operadores": result["oab_operadores"], "OCO_BKO": result["bko_alerta"], "OCO_Analitico": result["oab_report"], "OQL_Operadores": result["omc_operadores"], "OQL_Analitico": result["omc_report"]})
    st.download_button("📥 Baixar Excel completo da C6 Operação", data=excel_bytes, file_name=f"c6_operacao_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)

# =========================================================
# APP
# =========================================================
st.set_page_config(page_title="Assis e Mollerke · C6", layout="wide")
apply_theme()

if not login_gate():
    st.stop()

show_logo_and_title()
st.divider()
user_role = st.session_state.get("user_role", "admin")
operator_filter = st.session_state.get("operator_filter", "")
tab_labels = []
if user_role == "admin":
    tab_labels = ["📊 Painel C6 Empresas", "🎯 Meta Supervisor C6", "💬 Campanhas Meta", "📋 Leads Diários", "🏦 C6 Operação"]
elif user_role == "supervisor":
    tab_labels = ["🎯 Meta Supervisor C6", "🏦 C6 Operação"]
else:
    tab_labels = ["🏦 C6 Operação"]
tabs_map = dict(zip(tab_labels, st.tabs(tab_labels)))

# =========================================================
# =====================  TAB 1  ===========================
# ===================== PAINEL C6 ==========================
# =========================================================
if "📊 Painel C6 Empresas" in tabs_map:
  with tabs_map["📊 Painel C6 Empresas"]:

    st.subheader("Importação diária (Janeiro/26 em diante)")

    colA, colB = st.columns(2)
    with colA:
        up_c6 = st.file_uploader("Planilha C6 (Visão Cliente) — diária (.xlsx)", type=["xlsx"], accept_multiple_files=True, key="c6")
    with colB:
        up_leads = st.file_uploader("Planilha Leads — diária (.xlsx)", type=["xlsx"], accept_multiple_files=True, key="leads")

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
        for f in _sort_uploaded_c6_files(up_c6):
            raw_c6_bytes = f.getvalue()
            df_c6 = read_excel_any(raw_c6_bytes)
            st.session_state["c6_daily_visao_df"] = df_c6.copy()
            st.session_state["c6_daily_visao_df__name"] = f.name
            st.session_state["c6_daily_visao_df__ts"] = dt.datetime.now().timestamp()
            _save_daily_import_cache("visao", f.name, raw_c6_bytes)

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

            _persist_visao_month_snapshot(df_c6)
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

    if up_leads:
        for f in _sort_uploaded_leads_files(up_leads):
            raw_leads_bytes = f.getvalue()
            df_leads = read_excel_any(raw_leads_bytes)
            st.session_state["c6_daily_leads_df"] = df_leads.copy()
            st.session_state["c6_daily_leads_df__name"] = f.name
            st.session_state["c6_daily_leads_df__ts"] = dt.datetime.now().timestamp()
            _save_daily_import_cache("leads", f.name, raw_leads_bytes)
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
            if _cmp_day is None:
                _cmp_day = detect_report_day_from_df(df_leads)

    st.divider()

    _ = recompute_incremental()
    _ = recompute_cartilha_nova()
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

            mes_df = base_conv[base_conv["Mes_ref"] == mes_escolhido].copy()

            total_cad_mes = int(mes_df["Cadastradas"].sum())
            total_ab_mes = int(mes_df["Abertas"].sum())
            conv_mes = (total_ab_mes / total_cad_mes) if total_cad_mes > 0 else 0.0

            badge_mes = "am-badge-ok" if conv_mes >= ALVO_CONVERSAO else "am-badge-bad"
            st.markdown(
                f"<div class='{badge_mes}'>Conversão do mês selecionado ({fmt_month(mes_escolhido)}): "
                f"{str(round(conv_mes*100,1)).replace('.',',')}%</div>",
                unsafe_allow_html=True
            )

            c1, c2, c3 = st.columns(3)
            c1.metric("Mês selecionado", fmt_month(mes_escolhido))
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
            st.caption(f"📌 Arquivo do dia: {fmt_date(_rep_day)}  |  Mês do relatório: {_rep_month_lbl}")
        else:
            st.caption(f"📌 Mês do relatório: {_rep_month_lbl}")

        tab_ab, tab_fd, tab_px, tab_qv = st.tabs([
            "Aberturas",
            "Fundações (por dia)",
            "Pix + Status",
            "Qualificação + BR + Valores",
        ])

        with tab_ab:
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

        with tab_fd:
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

        with tab_px:
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

        with tab_qv:
            dqq = _df.copy()
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

    st.subheader("Cartilha nova (abril a junho/26)")

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

        n5, n6, n7, n8, n9 = st.columns(5)
        n5.metric("Cash In", br_int(int(info_novo.get("cash_in", 0))))
        n6.metric("Spending", br_int(int(info_novo.get("spending", 0))))
        n7.metric("C6 Pay", br_int(int(info_novo.get("c6pay", 0))))
        n8.metric("PIX CNPJ", br_int(int(info_novo.get("pix_cnpj", 0))))
        n9.metric("Wallet", br_int(int(info_novo.get("wallet", 0))))

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

    st.divider()

    st.subheader("Campanha 2º tri/26 (acompanhamento)")

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
            render_downloadable_table(df_liq, "painel_receita_liquida", "painel_receita_liquida", raw_df=df_liq)

    st.divider()

    st.subheader("Comparativo mensal de remuneração (regra antiga)")

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

        render_downloadable_table(view, "painel_remuneracao_antiga", "painel_remuneracao_antiga", raw_df=dfm)

        last = dfm.iloc[-1]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Último mês", str(last["Mês"]))
        c2.metric("Qualificadas", br_int(int(last["Qualificadas"])))
        c3.metric("Receita cheia", br_money(float(last["Deveria receber (cheio)"])))
        c4.metric("A receber", br_money(float(last["A receber no mês"])))




# =========================================================
# =====================  TAB 4  ===========================
# ================== 🏦 C6 OPERAÇÃO =======================
# =========================================================
if "🏦 C6 Operação" in tabs_map:
  with tabs_map["🏦 C6 Operação"]:
    _render_c6_operacao_tab(view_only=(user_role != "admin"), operator_filter=operator_filter if user_role == "operador" else "")

# =========================================================
# =====================  TAB 2  ===========================
# ================ 💬 Campanhas Meta =======================
# =========================================================
if "🎯 Meta Supervisor C6" in tabs_map:
  with tabs_map["🎯 Meta Supervisor C6"]:
    st.subheader("Meta Supervisor C6 Empresas")
    st.caption("Acompanhamento separado da meta do supervisor com base no Visao Cliente.")
    st.caption(f"Meta de contas abertas: {br_int(int(SUPERVISOR_C6_METAS['contas_abertas']['meta']))} | Premio: {br_money(float(SUPERVISOR_C6_METAS['contas_abertas']['premio']))}")
    st.caption("Contas qualificadas: faixa inicial de 700 = R$ 400,00, com progressao para 800, 900 e 1.000.")

    df_supervisor, df_supervisor_mes, sup_summary = compute_supervisor_c6_meta()

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

        st.divider()

        view_supervisor = _format_supervisor_indicator_view(df_supervisor)
        render_downloadable_table(view_supervisor, "sup_indicadores", "supervisor_indicadores", raw_df=df_supervisor)
        st.caption("Contas qualificadas têm prêmio progressivo: 700 = R$ 400,00 | 800 = R$ 500,00 | 900 = R$ 540,00 | 1.000 = R$ 1.400,00.")

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
        st.caption("Wallet continua zerado por enquanto, ate o banco liberar essa visao no relatorio.")

        pdf_bytes = _supervisor_pdf_bytes(str(sup_summary.get("report_day", "")), sup_summary, view_supervisor, view_supervisor_mes)
        st.download_button(
            "Baixar PDF do supervisor",
            data=pdf_bytes,
            file_name=f"meta_supervisor_c6_empresas_{str(sup_summary.get('report_day', '')).replace('/', '-') or 'atual'}.pdf",
            mime="application/pdf",
            use_container_width=False,
        )

        if user_role == "admin":
            st.divider()

            st.markdown("**Central de envio por e-mail**")
            email_cfg = _load_supervisor_email_cfg()
            c6_last = st.session_state.get("c6_operacao_last_result") or {}
            report_day = str(sup_summary.get("report_day", ""))

            report_options = []
            report_options.append({
                "key": "supervisor_pdf",
                "label": "Supervisor C6 (PDF)",
                "filename": _supervisor_email_filename(report_day),
                "maintype": "application",
                "subtype": "pdf",
                "data": pdf_bytes,
            })
            report_options.append({
                "key": "supervisor_excel",
                "label": "Supervisor C6 (Excel)",
                "filename": f"meta_supervisor_c6_empresas_{report_day.replace('/', '-') or 'atual'}.xlsx",
                "maintype": "application",
                "subtype": "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "data": _to_excel_bytes({"Supervisor": df_supervisor, "Evolucao_Diaria": df_supervisor_mes}),
            })
            if c6_last:
                act_df = _operator_pdf_view("act", c6_last.get("act_operadores", pd.DataFrame()))
                oco_df = _operator_pdf_view("oco", c6_last.get("oab_operadores", pd.DataFrame()))
                oql_df = _operator_pdf_view("oql", c6_last.get("omc_operadores", pd.DataFrame()))
                report_options.extend([
                    {
                        "key": "act_pdf",
                        "label": "Operadores ACT (PDF)",
                        "filename": f"operadores_act_{report_day.replace('/', '-') or 'atual'}.pdf",
                        "maintype": "application",
                        "subtype": "pdf",
                        "data": _report_pdf_bytes("Operadores ACT - C6 Empresas", report_day, act_df),
                    },
                    {
                        "key": "act_excel",
                        "label": "Operadores ACT (Excel)",
                        "filename": f"operadores_act_{report_day.replace('/', '-') or 'atual'}.xlsx",
                        "maintype": "application",
                        "subtype": "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        "data": _to_excel_bytes({"ACT_Operadores": c6_last.get("act_operadores", pd.DataFrame()), "ACT_Analitico": c6_last.get("act_report", pd.DataFrame())}),
                    },
                    {
                        "key": "oco_pdf",
                        "label": "Operadores OCO (PDF)",
                        "filename": f"operadores_oco_{report_day.replace('/', '-') or 'atual'}.pdf",
                        "maintype": "application",
                        "subtype": "pdf",
                        "data": _report_pdf_bytes("Operadores OCO - C6 Empresas", report_day, oco_df),
                    },
                    {
                        "key": "oco_excel",
                        "label": "Operadores OCO (Excel)",
                        "filename": f"operadores_oco_{report_day.replace('/', '-') or 'atual'}.xlsx",
                        "maintype": "application",
                        "subtype": "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        "data": _to_excel_bytes({"OCO_Operadores": c6_last.get("oab_operadores", pd.DataFrame()), "OCO_Analitico": c6_last.get("oab_report", pd.DataFrame()), "BKO_5mais_dias": c6_last.get("bko_alerta", pd.DataFrame())}),
                    },
                    {
                        "key": "oql_pdf",
                        "label": "Operadores OQL (PDF)",
                        "filename": f"operadores_oql_{report_day.replace('/', '-') or 'atual'}.pdf",
                        "maintype": "application",
                        "subtype": "pdf",
                        "data": _report_pdf_bytes("Operadores OQL - C6 Empresas", report_day, oql_df),
                    },
                    {
                        "key": "oql_excel",
                        "label": "Operadores OQL (Excel)",
                        "filename": f"operadores_oql_{report_day.replace('/', '-') or 'atual'}.xlsx",
                        "maintype": "application",
                        "subtype": "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        "data": _to_excel_bytes({"OQL_Operadores": c6_last.get("omc_operadores", pd.DataFrame()), "OQL_Analitico": c6_last.get("omc_report", pd.DataFrame())}),
                    },
                ])

            st.caption("Selecione abaixo quais relatórios você quer enviar.")
            opt_cols = st.columns(2)
            selected_reports = []
            for i, opt in enumerate(report_options):
                with opt_cols[i % 2]:
                    csel, cdl = st.columns([4, 1])
                    with csel:
                        checked = st.checkbox(opt["label"], value=(opt["key"] == "supervisor_pdf"), key=f"mail_opt_{opt['key']}")
                    if checked:
                        selected_reports.append(opt)
                        if opt["subtype"] == "pdf":
                            with cdl:
                                st.download_button(
                                    "Baixar PDF",
                                    data=opt["data"],
                                    file_name=opt["filename"],
                                    mime=f"{opt['maintype']}/{opt['subtype']}",
                                    key=f"preview_mail_inline_{opt['key']}",
                                    use_container_width=True,
                                )

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
            st.caption("Anexos selecionados:")
            for opt in selected_reports:
                st.caption(f"- {opt['filename']}")
            st.caption(f"Cópia automática: {SMTP_SENDER}")

            if st.button("Enviar e-mail agora", key="send_supervisor_email_btn"):
                _save_supervisor_email_cfg(to_email, smtp_password)
                pwd = str(smtp_password or "").strip() or _smtp_password_from_secrets()
                if not str(to_email or "").strip():
                    st.error("Preencha o e-mail de destino.")
                elif not pwd:
                    st.error("Preencha a senha do e-mail remetente para enviar.")
                elif not selected_reports:
                    st.error("Selecione ao menos um relatório para enviar.")
                else:
                    try:
                        send_email_with_attachments(
                            to_email=str(to_email).strip(),
                            smtp_password=pwd,
                            subject=str(subject or subj_default).strip(),
                            body=str(body or body_default).strip(),
                            attachments=selected_reports,
                        )
                        st.success("E-mail enviado com sucesso.")
                    except Exception as e:
                        st.error(f"Não consegui enviar o e-mail: {e}")

if "💬 Campanhas Meta" in tabs_map:
  with tabs_map["💬 Campanhas Meta"]:
    st.subheader("💬 Campanhas Meta")

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
            st.warning(f"⚠️ {qtd_substituidos} arquivo(s) foram reimportados com dados diferentes (substituindo versão anterior).")
        if qtd_mesmo > 0:
            st.info(f"🔁 {qtd_mesmo} arquivo(s) reimportado(s) (mesmo conteúdo) — reprocessado(s) sem bloqueio.")
        if qtd_novos > 0:
            st.info(f"📁 {qtd_novos} novo(s) arquivo(s) adicionados.")

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
                        f"✅ Importação concluída: {qtd_novos} novo(s), {qtd_substituidos} substituído(s), {qtd_mesmo} reimportado(s) (mesmo conteúdo)."
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
                        st.warning(f"⚠️ {qtd_substituidos} arquivo(s) substituídos por versões mais recentes.")
                    if qtd_mesmo > 0:
                        st.info(f"🔁 {qtd_mesmo} arquivo(s) reimportado(s) (mesmo conteúdo) — reprocessado(s) sem bloqueio.")

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
                                    f"✅ Grupos atualizados: {qtd_novos} novo(s), {qtd_substituidos} substituído(s), {qtd_mesmo} reimportado(s) (mesmo conteúdo)."
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
if "📋 Leads Diários" in tabs_map:
  with tabs_map["📋 Leads Diários"]:

    st.subheader("📋 Leads Diários (Status por Data Base)")

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
        if df_obj is not None:
            return df_obj.copy(), name, "Painel C6 Empresas (sessão atual)"

        df_cache, cache_name, cache_origin = _load_daily_import_cache(kind)
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

            st.success(f"✅ {processados} arquivo(s) processado(s). (erros: {erros})")

            st.session_state["leads_upload_seq"] += 1
            st.rerun()

    store = _leads_status_load() or {}
    control = safe_json_load(LEADS_CONTROL_PATH, default={}) or {}

    df_panel_leads, panel_leads_name, panel_leads_origin = _pick_latest_panel_df("leads")
    df_panel_visao, panel_visao_name, panel_visao_origin = _pick_latest_panel_df("visao")

    st.caption("Esta aba reaproveita automaticamente os arquivos de Leads e Visão Cliente importados no Painel C6 Empresas.")
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
                _save_daily_import_cache("lct", up_lct.name, raw_lct_bytes)
                st.success("Resumo LCT importado.")

        df_panel_lct, panel_lct_name, panel_lct_origin = _load_daily_import_cache("lct")
        if panel_lct_name:
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
        st.markdown("### ⏱️ Tempo até abertura da conta")

        opened_leads = leads_funil[leads_funil["abriu_conta"] & leads_funil["dias_para_abrir"].notna()].copy()
        avg_open_days = float(opened_leads["dias_para_abrir"].mean()) if not opened_leads.empty else 0.0
        med_open_days = float(opened_leads["dias_para_abrir"].median()) if not opened_leads.empty else 0.0
        within_15 = int(opened_leads["dias_para_abrir"].fillna(9999).le(15).sum()) if not opened_leads.empty else 0
        total_opened = int(opened_leads["cnpj"].nunique()) if not opened_leads.empty else 0

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
        st.markdown("#### 📄 Analítico de prazo do lead")
        render_downloadable_table(detalhe_leads, "leads_analitico_prazo", "leads_analitico_prazo", raw_df=detalhe_leads)

        if 'df_panel_lct' in locals() and df_panel_lct is not None and not df_panel_lct.empty:
            st.divider()
            st.markdown("### ☎️ Clientes da URA")

            raw_nome_col = _coalesce_col(df_panel_lct, ["Nome", "NOME", "NOME_CLIENTE"])
            raw_cnpj_col = _coalesce_col(df_panel_lct, ["CPF / CNPJ", "CNPJ", "CNPJ_CLIENTE"])
            raw_data_col = _coalesce_col(df_panel_lct, ["Data", "DATA"])
            raw_fase_col = _coalesce_col(df_panel_lct, ["Fase", "FASE"])
            raw_acao_col = _coalesce_col(df_panel_lct, ["Ação", "ACAO", "Acao"])

            if raw_cnpj_col and raw_data_col:
                lct_work = pd.DataFrame()
                lct_work["cnpj"] = _normalize_cnpj_series(df_panel_lct[raw_cnpj_col])
                lct_work["nome_cliente_lct"] = normalize_str(df_panel_lct[raw_nome_col]) if raw_nome_col else ""

                raw_data_txt = df_panel_lct[raw_data_col].astype("string").fillna("").str.strip()
                lct_work["data_lct"] = pd.to_datetime(
                    raw_data_txt.str.extract(r"(\d{2}/\d{2}/\d{4})", expand=False).fillna(raw_data_txt),
                    errors="coerce",
                    dayfirst=True
                )
                lct_work["data_lct_dia"] = lct_work["data_lct"].dt.date

                if raw_fase_col:
                    raw_fase_txt = df_panel_lct[raw_fase_col].astype("string").fillna("").str.strip()
                    lct_work["dt_fundacao_lct"] = pd.to_datetime(
                        raw_fase_txt.str.extract(r"(\d{2}/\d{2}/\d{4})", expand=False).fillna(raw_fase_txt),
                        errors="coerce",
                        dayfirst=True
                    )
                else:
                    lct_work["dt_fundacao_lct"] = pd.NaT

                lct_work = lct_work[lct_work["cnpj"] != ""].copy()
                lct_work = lct_work[lct_work["data_lct_dia"].notna()].copy()
                lct_work = lct_work.sort_values(["data_lct", "cnpj"]).drop_duplicates(subset=["cnpj", "data_lct_dia"], keep="last")

                if not lct_work.empty:
                    leads_idx = leads_funil[["cnpj", "data_hora_cadastro", "status_abertura_conta", "pendencias"]].copy()
                    visao_idx = visao_funil[["cnpj", "dt_conta_criada", "nome_cliente", "dt_fundacao_empresa"]].copy() if not visao_funil.empty else pd.DataFrame(columns=["cnpj", "dt_conta_criada", "nome_cliente", "dt_fundacao_empresa"])

                    lct_merged = lct_work.merge(leads_idx, on="cnpj", how="left").merge(visao_idx, on="cnpj", how="left")
                    lct_merged["nome_cliente_final"] = lct_merged["nome_cliente_lct"].replace("", pd.NA).fillna(lct_merged["nome_cliente"])
                    lct_merged["fundacao_final"] = lct_merged["dt_fundacao_empresa"]
                    fund_mask = pd.to_datetime(lct_merged["fundacao_final"], errors="coerce").isna()
                    lct_merged.loc[fund_mask, "fundacao_final"] = lct_merged.loc[fund_mask, "dt_fundacao_lct"]
                    lct_merged["indicado_banco"] = lct_merged["data_hora_cadastro"].notna()
                    lct_merged["abriu_conta"] = lct_merged["dt_conta_criada"].notna()

                    ura_daily = (
                        lct_work.groupby("data_lct_dia", dropna=True)
                        .agg(clientes_ura=("cnpj", "nunique"))
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
                    ura_daily = ura_daily.merge(indicados_daily, on="data_lct_dia", how="left").merge(aberturas_daily, on="data_lct_dia", how="left")
                    ura_daily["clientes_indicados"] = pd.to_numeric(ura_daily["clientes_indicados"], errors="coerce").fillna(0).astype(int)
                    ura_daily["contas_abertas"] = pd.to_numeric(ura_daily["contas_abertas"], errors="coerce").fillna(0).astype(int)
                    ura_daily["% viraram indicação"] = (ura_daily["clientes_indicados"] / ura_daily["clientes_ura"].replace(0, pd.NA) * 100).fillna(0)
                    ura_daily["% abriram conta"] = (ura_daily["contas_abertas"] / ura_daily["clientes_ura"].replace(0, pd.NA) * 100).fillna(0)

                    view_ura = ura_daily.copy()
                    view_ura["Data"] = pd.to_datetime(view_ura["data_lct_dia"], errors="coerce").dt.strftime("%d/%m/%Y")
                    for col in ["clientes_ura", "clientes_indicados", "contas_abertas"]:
                        view_ura[col] = pd.to_numeric(view_ura[col], errors="coerce").fillna(0).astype(int).apply(br_int)
                    for col in ["% viraram indicação", "% abriram conta"]:
                        view_ura[col] = view_ura[col].apply(lambda x: f"{float(x):.1f}%".replace(".", ","))
                    view_ura = view_ura.rename(columns={
                        "clientes_ura": "Clientes URA",
                        "clientes_indicados": "Indicados no banco",
                        "contas_abertas": "Abriram conta",
                    })[["Data", "Clientes URA", "Indicados no banco", "Abriram conta", "% viraram indicação", "% abriram conta"]]
                    render_downloadable_table(view_ura, "leads_lct_resumo", "leads_clientes_ura", raw_df=view_ura)

                    analitico_ura = lct_merged.copy()
                    analitico_ura["Data URA"] = pd.to_datetime(analitico_ura["data_lct"], errors="coerce").dt.strftime("%d/%m/%Y")
                    analitico_ura["Fundação empresa"] = pd.to_datetime(analitico_ura["fundacao_final"], errors="coerce").dt.strftime("%d/%m/%Y")
                    analitico_ura["Data cadastro banco"] = pd.to_datetime(analitico_ura["data_hora_cadastro"], errors="coerce").dt.strftime("%d/%m/%Y")
                    analitico_ura["Data conta criada"] = pd.to_datetime(analitico_ura["dt_conta_criada"], errors="coerce").dt.strftime("%d/%m/%Y")
                    analitico_ura["Indicado no banco"] = analitico_ura["indicado_banco"].map({True: "SIM", False: "NÃO"})
                    analitico_ura["Abriu conta"] = analitico_ura["abriu_conta"].map({True: "SIM", False: "NÃO"})
                    analitico_ura["CNPJ"] = analitico_ura["cnpj"]
                    analitico_ura["Nome cliente"] = analitico_ura["nome_cliente_final"]
                    analitico_ura = analitico_ura[["Data URA", "Nome cliente", "CNPJ", "Fundação empresa", "Indicado no banco", "Data cadastro banco", "Abriu conta", "Data conta criada", "status_abertura_conta", "pendencias"]].rename(columns={"status_abertura_conta": "Status abertura", "pendencias": "Pendências"})
                    render_downloadable_table(analitico_ura, "leads_lct_analitico", "leads_clientes_ura_analitico", raw_df=analitico_ura)
                else:
                    st.info("O Resumo LCT foi lido, mas não encontrei linhas válidas com CNPJ e Data.")
            else:
                st.info("O Resumo LCT foi lido, mas não identifiquei as colunas obrigatórias de CNPJ e Data.")
        elif 'df_panel_lct' in locals() and df_panel_lct is not None and df_panel_lct.empty:
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
            st.markdown("### ⛳ Tempos médios do cliente após a abertura")
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
            st.markdown("#### 📌 Onde o cliente já avançou no funil")
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
            st.markdown("#### 📄 Médias de tempo do funil")
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

            st.markdown("### 📊 Resumo Geral")
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
                st.metric("📊 Total de Leads", br_int(total_geral))
            with col_metric4:
                st.metric("✅ Indicações Válidas (≤14d)", br_int(total_validas))

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
        if st.button("🧹 Resetar somente Leads – Status Diário", use_container_width=True, type="secondary"):
            _leads_status_reset_only()

st.divider()
st.markdown("### Reset geral")
col_reset_app_1, col_reset_app_2, col_reset_app_3 = st.columns([1, 2, 1])
with col_reset_app_2:
    if st.button("🔄 Resetar histórico do app", use_container_width=True, type="secondary"):
        reset_all_data()
        st.success("Histórico resetado. Reimporte Nov/25 e Dez/25, se precisar, e depois os arquivos diários.")
