import os
import re
import io
import html
import traceback
import unicodedata
from collections import Counter
from datetime import datetime

import streamlit as st
import pdfplumber
import pandas as pd

st.set_page_config(
    page_title="Conversor Extrato PDF para OFX",
    page_icon="🏦",
    layout="wide"
)


class BankParsers:

    # ══════════════════════════════════════════════════════════════
    # UTILITÁRIOS
    # ══════════════════════════════════════════════════════════════

    @staticmethod
    def _parse_date(dt_str):
        current_year = datetime.now().year
        parts = dt_str.strip().split("/")
        if len(parts) == 2:
            dt_formatted = f"{parts[0]}/{parts[1]}/{current_year}"
        elif len(parts) == 3 and len(parts[2]) == 2:
            dt_formatted = f"{parts[0]}/{parts[1]}/20{parts[2]}"
        else:
            dt_formatted = dt_str.strip()
        try:
            return datetime.strptime(dt_formatted, "%d/%m/%Y")
        except ValueError:
            return None

    @staticmethod
    def _extract_initial_balance(text_lines):
        pattern_saldo = re.compile(
            r"(?:SALDO\s+ANTERIOR|SD\s+CTA/APL|SALDO\s+INICIAL)"
            r"\s*[:\.-]?\s*(-?[\d\.]+\,\d{2})",
            re.IGNORECASE
        )
        for line in text_lines:
            match = pattern_saldo.search(line.strip())
            if match:
                try:
                    return float(
                        match.group(1).replace(".", "").replace(",", "."))
                except ValueError:
                    continue
        return 0.0

    @staticmethod
    def _extract_account(text_lines):
        """Retorna (agencia, conta) se encontrar no cabeçalho."""
        txt = BankParsers._normalize(" ".join(text_lines[:20]))
        m = re.search(r"AGENCIA:\s*(\d+).*?CONTA:\s*([\d\-]+)", txt)
        if m:
            return m.group(1), m.group(2).replace("-", "")
        return None, None

    @staticmethod
    def _normalize(text):
        return (
            unicodedata.normalize("NFD", text)
            .encode("ascii", "ignore")
            .decode()
            .upper()
        )

    @staticmethod
    def _group_lines(words, tol=3.0):
        """Agrupa palavras do pdfplumber em linhas visuais (centro vertical)."""
        lines = []
        for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
            cy = (w["top"] + w["bottom"]) / 2
            for ln in lines:
                if abs(ln["cy"] - cy) <= tol:
                    ln["words"].append(w)
                    break
            else:
                lines.append({"cy": cy, "words": [w]})
        for ln in lines:
            ln["words"].sort(key=lambda w: w["x0"])
            ln["top"] = min(w["top"] for w in ln["words"])
            ln["bottom"] = max(w["bottom"] for w in ln["words"])
            ln["text"] = " ".join(w["text"] for w in ln["words"])
        lines.sort(key=lambda l: l["cy"])
        return lines

    # ══════════════════════════════════════════════════════════════
    # SANTANDER — Internet Banking Empresarial
    # ══════════════════════════════════════════════════════════════

    SANT_RE_DATE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
    SANT_RE_VAL = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d{2}$")
    SANT_RE_FLAG = re.compile(r"^[abp]$")      # a/b/p = bloqueio/provisionado
    SANT_IGNORE = ("RESGATE CONTAMAX", "APLICACAO CONTAMAX")
    SANT_SKIP = ("SALDO ANTERIOR", "SALDO DIA")
    SANT_FOOTER = ("A - SALDO", "A = BLOQUEIO", "CENTRAL DE ATENDIMENTO")

    @staticmethod
    def santander(text_lines, pdf_bytes=None, ignorar_contamax=True):
        """
        1ª tentativa: coordenadas das palavras (robusto a histórico multi-linha).
        2ª tentativa: texto do extract_text (fallback).
        """
        records = []
        if pdf_bytes:
            try:
                records = BankParsers._santander_por_coordenadas(pdf_bytes)
            except Exception:
                traceback.print_exc()
                records = []
        if not records:
            records = BankParsers._santander_por_texto(text_lines)
        return BankParsers._santander_montar(records, ignorar_contamax)

    @staticmethod
    def _santander_por_coordenadas(pdf_bytes):
        """
        Cada data da coluna esquerda é uma 'âncora'. Toda palavra da área
        útil é atribuída à âncora verticalmente mais próxima — funciona para
        histórico de 1, 2 ou 3 linhas (centralizado na linha da data) e para
        páginas sem cabeçalho.
        """
        norm = BankParsers._normalize
        RE_DATE = BankParsers.SANT_RE_DATE
        RE_VAL = BankParsers.SANT_RE_VAL
        RE_FLAG = BankParsers.SANT_RE_FLAG
        MAX_DIST = 25.0  # pt — distância máxima palavra ↔ data

        records = []
        header = {}  # posições do cabeçalho (reaproveitadas nas páginas seguintes)

        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                W, H = float(page.width), float(page.height)
                words = page.extract_words(
                    x_tolerance=2, y_tolerance=2,
                    keep_blank_chars=False, use_text_flow=False,
                )
                if not words:
                    continue
                lines = BankParsers._group_lines(words)

                # ── Cabeçalho (só existe na 1ª página) ────────────
                y_start = 0.0
                for ln in lines:
                    toks = {norm(w["text"]) for w in ln["words"]}
                    if {"DATA", "VALOR", "SALDO"} <= toks:
                        y_start = ln["bottom"]
                        for w in ln["words"]:
                            header[norm(w["text"])] = w
                        break

                # ── Rodapé (resumo de saldos / atendimento) ───────
                y_end = H
                for ln in lines:
                    if (ln["top"] > y_start and
                            norm(ln["text"]).startswith(
                                BankParsers.SANT_FOOTER)):
                        y_end = ln["top"]
                        break

                # ── Limites horizontais ───────────────────────────
                # Documento/Valor/Saldo ficam à direita de doc_split
                doc_split = (header["DOCUMENTO"]["x0"] - 10
                             if "DOCUMENTO" in header else W * 0.47)
                # Valor é alinhado à direita; Saldo fica depois do centro
                # do título "Saldo"
                saldo_split = (
                    (header["SALDO"]["x0"] + header["SALDO"]["x1"]) / 2
                    if "SALDO" in header else W * 0.85)

                body = [w for w in words
                        if y_start < w["top"] < y_end]

                # ── Âncoras (datas na coluna esquerda) ────────────
                anchors, anchor_ids = [], set()
                for w in body:
                    if w["x0"] < W * 0.25 and RE_DATE.match(w["text"]):
                        anchors.append({
                            "date": w["text"],
                            "cy": (w["top"] + w["bottom"]) / 2,
                            "words": [],
                        })
                        anchor_ids.add(id(w))
                if not anchors:
                    continue

                # ── Atribui cada palavra à data mais próxima ──────
                for w in body:
                    if id(w) in anchor_ids:
                        continue
                    cy = (w["top"] + w["bottom"]) / 2
                    best = min(anchors, key=lambda a: abs(a["cy"] - cy))
                    if abs(best["cy"] - cy) <= MAX_DIST:
                        best["words"].append(w)

                # ── Início da coluna Histórico (moda do x0) ───────
                starts = Counter()
                for a in anchors:
                    zone = [w for w in a["words"] if w["x0"] < doc_split]
                    for ln in BankParsers._group_lines(zone):
                        first = ln["words"][0]
                        if not RE_FLAG.match(first["text"]):
                            starts[round(first["x0"])] += 1
                hist_left = starts.most_common(1)[0][0] if starts else None

                # ── Monta cada lançamento ─────────────────────────
                for a in anchors:
                    hist_words, right, flag = [], [], ""
                    for w in a["words"]:
                        if w["x0"] >= doc_split:
                            right.append(w)
                        elif (RE_FLAG.match(w["text"])
                              and hist_left is not None
                              and w["x1"] < hist_left - 2):
                            flag = w["text"]
                        else:
                            hist_words.append(w)

                    vals = sorted(
                        (w for w in right if RE_VAL.match(w["text"])),
                        key=lambda w: w["x0"])
                    valor = next(
                        (w for w in vals if w["x1"] < saldo_split), None)
                    if valor is None:
                        continue  # ex.: SALDO ANTERIOR (sem valor)

                    desc = " ".join(
                        ln["text"]
                        for ln in BankParsers._group_lines(hist_words))
                    records.append({
                        "date": a["date"],
                        "val": valor["text"],
                        "desc": re.sub(r"\s{2,}", " ", desc).strip(),
                        "flag": flag,
                    })
        return records

    @staticmethod
    def _santander_por_texto(text_lines):
        """
        Fallback pelo extract_text: histórico multi-linha é centralizado na
        linha da data → N linhas antes da data = N linhas depois.
        """
        norm = BankParsers._normalize
        RE_NOISE = re.compile(
            r"^(?:INTERNET BANKING|CONTA CORRENTE >|PERIODO:|DATA HISTORICO"
            r"|[A-F] - SALDO|[ABP] = |CENTRAL DE ATENDIMENTO|DAS \d+H"
            r"|4004 |0800 |55 \(11\)|SAC\b|OUVIDORIA|CANAL EXCLUSIVO"
            r"|HTTPS?:|ATENDIMENTO\b)"
        )
        NOISE_CONTAINS = ("AGENCIA:", "DATA/HORA:")
        VAL = r"-?\d{1,3}(?:\.\d{3})*,\d{2}"
        RE_DATE_START = re.compile(r"^\d{2}/\d{2}/\d{4}\b")
        RE_TX = re.compile(
            r"^(?P<date>\d{2}/\d{2}/\d{4})"
            r"(?:\s+(?P<flag>[abp])(?=\s))?"
            r"\s+(?P<hist>.*?)\s*"
            r"(?P<doc>\b\d{6})?"
            rf"\s+(?P<val>{VAL})"
            rf"(?:\s+(?P<saldo>{VAL}))?\s*$"
        )

        records, pending, current, suffix_needed = [], [], None, 0
        for raw in text_lines:
            line = re.sub(r"\s{2,}", " ", raw).strip()
            if not line:
                continue
            n = norm(line)

            if RE_DATE_START.match(line):
                m = RE_TX.match(line)
                if m:
                    parts = list(pending)
                    if m.group("hist"):
                        parts.append(m.group("hist").strip())
                    current = {"date": m.group("date"),
                               "val": m.group("val"),
                               "parts": parts,
                               "flag": m.group("flag") or ""}
                    records.append(current)
                    suffix_needed = len(pending)
                else:
                    current, suffix_needed = None, 0
                pending = []
                continue

            if RE_NOISE.match(n) or any(k in n for k in NOISE_CONTAINS):
                pending = []
                continue

            if current is not None and suffix_needed > 0:
                current["parts"].append(line)
                suffix_needed -= 1
                continue

            pending.append(line)

        for r in records:
            r["desc"] = " ".join(r.pop("parts")).strip()
        return records

    @staticmethod
    def _santander_montar(records, ignorar_contamax):
        norm = BankParsers._normalize
        transactions = []
        for r in records:
            desc = r["desc"]
            d = norm(desc)
            if not desc:
                continue
            if any(s in d for s in BankParsers.SANT_SKIP):
                continue
            if ignorar_contamax and any(
                    s in d for s in BankParsers.SANT_IGNORE):
                continue
            dt_obj = BankParsers._parse_date(r["date"])
            if not dt_obj:
                continue
            try:
                val = float(r["val"].replace(".", "").replace(",", "."))
            except ValueError:
                continue
            transactions.append({
                "date_obj": dt_obj,
                "amount": val,
                "description": desc,
            })
        return transactions

    # ══════════════════════════════════════════════════════════════
    # OUTROS BANCOS
    # ══════════════════════════════════════════════════════════════

    @staticmethod
    def itau(text_lines):
        transactions = []
        pattern = re.compile(
            r"(\d{2}/\d{2}(?:/\d{2,4})?)\s+(.+?)"
            r"\s+(-?[\d\.]+\,\d{2})\s*([CD])?",
            re.IGNORECASE
        )
        for line in text_lines:
            s = line.strip()
            if any(t in s.upper() for t in
                   ["SALDO DA CONTA", "SD CTA/APL", "SALDO ANTERIOR"]):
                continue
            m = pattern.search(s)
            if m:
                dt_str, desc, val_str, tp = m.groups()
                dt_obj = BankParsers._parse_date(dt_str)
                if not dt_obj:
                    continue
                val = float(val_str.replace(".", "").replace(",", "."))
                if tp:
                    val = -abs(val) if tp.upper() == "D" else abs(val)
                transactions.append({
                    "date_obj": dt_obj, "amount": val,
                    "description": desc.strip()
                })
        return transactions

    @staticmethod
    def banco_do_brasil(text_lines):
        transactions = []
        pattern = re.compile(
            r"(\d{2}/\d{2}(?:/\d{2,4})?)\s+(.+?)"
            r"\s+([\d\.]+\,\d{2})\s*([CD])",
            re.IGNORECASE
        )
        for line in text_lines:
            s = line.strip()
            if any(t in s.upper() for t in
                   ["SALDO ANTERIOR", "S A L D O", "RESUMO"]):
                continue
            m = pattern.search(s)
            if m:
                dt_str, desc, val_str, tp = m.groups()
                dt_obj = BankParsers._parse_date(dt_str)
                if not dt_obj:
                    continue
                val = float(val_str.replace(".", "").replace(",", "."))
                if tp.upper() == "D":
                    val = -abs(val)
                transactions.append({
                    "date_obj": dt_obj, "amount": val,
                    "description": desc.strip()
                })
        return transactions

    @staticmethod
    def bradesco(text_lines):
        transactions = []
        pattern = re.compile(
            r"(\d{2}/\d{2}(?:/\d{2,4})?)\s+(.+?)"
            r"\s+(-?[\d\.]+\,\d{2})([\+-])?",
            re.IGNORECASE
        )
        for line in text_lines:
            s = line.strip()
            if any(t in s.upper() for t in
                   ["SALDO ANTERIOR", "ULTIMO SALDO"]):
                continue
            m = pattern.search(s)
            if m:
                dt_str, desc, val_str, signal = m.groups()
                dt_obj = BankParsers._parse_date(dt_str)
                if not dt_obj:
                    continue
                val = float(val_str.replace(".", "").replace(",", "."))
                if signal == "-" or val_str.startswith("-"):
                    val = -abs(val)
                transactions.append({
                    "date_obj": dt_obj, "amount": val,
                    "description": desc.strip()
                })
        return transactions

    @staticmethod
    def caixas(text_lines):
        transactions = []
        pattern = re.compile(
            r"(\d{2}/\d{2}(?:/\d{2,4})?)\s*(?:\d+)?\s+(.+?)"
            r"\s+([\d\.]+\,\d{2})\s*([CD])",
            re.IGNORECASE
        )
        for line in text_lines:
            s = line.strip()
            if any(t in s.upper() for t in ["SALDO ANTER", "SALDO DIA"]):
                continue
            m = pattern.search(s)
            if m:
                dt_str, desc, val_str, tp = m.groups()
                dt_obj = BankParsers._parse_date(dt_str)
                if not dt_obj:
                    continue
                val = float(val_str.replace(".", "").replace(",", "."))
                if tp.upper() == "D":
                    val = -abs(val)
                transactions.append({
                    "date_obj": dt_obj, "amount": val,
                    "description": desc.strip()
                })
        return transactions

    @staticmethod
    def generic_fallback(text_lines):
        transactions = []
        pattern = re.compile(
            r"(\d{2}/\d{2}(?:/\d{2,4})?)\s+(.+?)"
            r"\s+(-?[\d\.]+\,\d{2})\s*([CD])?",
            re.IGNORECASE
        )
        for line in text_lines:
            s = line.strip()
            if any(t in s.upper() for t in
                   ["SALDO ANTERIOR", "RENDIMENTO", "TOTAL"]):
                continue
            m = pattern.search(s)
            if m:
                dt_str, desc, val_str, tp = m.groups()
                dt_obj = BankParsers._parse_date(dt_str)
                if not dt_obj:
                    continue
                val = float(val_str.replace(".", "").replace(",", "."))
                if tp and tp.upper() == "D":
                    val = -abs(val)
                transactions.append({
                    "date_obj": dt_obj, "amount": val,
                    "description": desc.strip()
                })
        return transactions


BANK_MAPPING = {
    "Itaú Unibanco (341)":           (BankParsers.itau,             "341"),
    "Bradesco (237)":                (BankParsers.bradesco,         "237"),
    "Santander (033)":               (BankParsers.santander,        "033"),
    "Banco do Brasil (001)":         (BankParsers.banco_do_brasil,  "001"),
    "Caixa Econômica Federal (104)": (BankParsers.caixas,           "104"),
    "Sicoob (756)":                  (BankParsers.generic_fallback, "756"),
    "Sicredi (748)":                 (BankParsers.generic_fallback, "748"),
    "Banco Inter (077)":             (BankParsers.generic_fallback, "077"),
    "Nubank (260)":                  (BankParsers.generic_fallback, "260"),
    "C6 Bank (336)":                 (BankParsers.generic_fallback, "336"),
    "Banrisul (041)":                (BankParsers.generic_fallback, "041"),
    "Stone Pagamentos (197)":        (BankParsers.generic_fallback, "197"),
    "Unicred (136)":                 (BankParsers.generic_fallback, "136"),
    "Mercado Pago (323)":            (BankParsers.generic_fallback, "323"),
}


def detect_bank(text_lines):
    header_text = " ".join(text_lines[:40]).upper()
    header_norm = (
        unicodedata.normalize("NFD", header_text)
        .encode("ascii", "ignore")
        .decode()
    )
    BANK_SIGNATURES = [
        ("Santander (033)",              [("SANTANDER", 3),
                                          ("CONTAMAX", 5),
                                          ("INTERNET BANKING EMPRESARIAL", 4),
                                          ("BLOQUEIO DIA", 3),
                                          ("4004 2125", 4),
                                          ("0800 702 2125", 4)]),
        ("Itaú Unibanco (341)",          [("ITAU UNIBANCO", 5),
                                          ("BANCO ITAU", 4),
                                          ("ITOKEN", 4),
                                          ("0300 789 8484", 4)]),
        ("Bradesco (237)",               [("BANCO BRADESCO", 5),
                                          ("BRADESCO S.A", 5),
                                          ("BRADESCO PRIME", 4),
                                          ("BRADESCO", 3),
                                          ("0800 704 8383", 4)]),
        ("Banco do Brasil (001)",        [("BANCO DO BRASIL", 5),
                                          ("BB.COM.BR", 5),
                                          ("0800 729 0722", 4),
                                          ("AGENCIA BB", 3)]),
        ("Caixa Econômica Federal (104)", [("CAIXA ECONOMICA FEDERAL", 5),
                                           ("CEF", 2),
                                           ("0800 726 0101", 4),
                                           ("CAIXA.GOV", 4)]),
        ("Sicoob (756)",                 [("SICOOB", 5),
                                          ("0800 642 2200", 4)]),
        ("Sicredi (748)",                [("SICREDI", 5),
                                          ("0800 724 7220", 4)]),
        ("Banco Inter (077)",            [("BANCO INTER", 5),
                                          ("INTER S.A", 4),
                                          ("CONTA DIGITAL INTER", 5)]),
        ("Nubank (260)",                 [("NUBANK", 5),
                                          ("NU PAGAMENTOS", 5),
                                          ("NUCONTA", 4)]),
        ("C6 Bank (336)",                [("C6 BANK", 5),
                                          ("C6 S.A", 4)]),
        ("Banrisul (041)",               [("BANRISUL", 5),
                                          ("BANCO DO ESTADO DO RIO GRANDE", 4)]),
        ("Stone Pagamentos (197)",       [("STONE PAGAMENTOS", 5),
                                          ("STONECO", 4)]),
        ("Unicred (136)",                [("UNICRED", 5)]),
        ("Mercado Pago (323)",           [("MERCADO PAGO", 5),
                                          ("MERCADOPAGO", 5)]),
    ]
    scores = {}
    for bank_key, signatures in BANK_SIGNATURES:
        total = sum(w for t, w in signatures if t in header_norm)
        if total > 0:
            scores[bank_key] = total
    return max(scores, key=lambda k: scores[k]) if scores else None


def _ofx_escape(text):
    return html.escape(str(text), quote=False)


def generate_ofx(transactions, bank_code="000",
                 acct_id=None, branch_id=None):
    now = datetime.now().strftime("%Y%m%d%H%M%S")
    dt_start = (transactions[0]["date_obj"].strftime("%Y%m%d")
                if transactions else now)
    dt_end = (transactions[-1]["date_obj"].strftime("%Y%m%d")
              if transactions else now)
    acct_id = acct_id or "00000000"
    branch_line = f"<BRANCHID>{branch_id}</BRANCHID>\n" if branch_id else ""

    ofx = f"""OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEDAREA:NONE
NEWFILEDAREA:NONE

<OFX>
<SIGNONMSGSRSV1>
<SONRS>
<STATUS>
<CODE>0
<SEVERITY>INFO
</STATUS>
<DTSERVER>{now}
<LANGUAGE>POR
</SONRS>
</SIGNONMSGSRSV1>
<BANKMSGSRSV1>
<STMTTRNRS>
<TRNUID>{now}
<STATUS>
<CODE>0
<SEVERITY>INFO
</STATUS>
<STMTRS>
<CURDEF>BRL</CURDEF>
<BANKACCTFROM>
<BANKID>{bank_code}</BANKID>
{branch_line}<ACCTID>{acct_id}</ACCTID>
<ACCTTYPE>CHECKING</ACCTTYPE>
</BANKACCTFROM>
<BANKTRANLIST>
<DTSTART>{dt_start}</DTSTART>
<DTEND>{dt_end}</DTEND>
"""
    for idx, tr in enumerate(transactions):
        tr_type = "CREDIT" if tr["amount"] > 0 else "DEBIT"
        dt_str = tr["date_obj"].strftime("%Y%m%d")
        ofx += f"""<STMTTRN>
<TRNTYPE>{tr_type}</TRNTYPE>
<DTPOSTED>{dt_str}</DTPOSTED>
<TRNAMT>{tr['amount']:.2f}</TRNAMT>
<FITID>{dt_str}{idx+1:04d}</FITID>
<MEMO>{_ofx_escape(tr['description'])}</MEMO>
</STMTTRN>
"""
    ofx += """</BANKTRANLIST>
</STMTRS>
</STMTTRNRS>
</BANKMSGSRSV1>
</OFX>"""
    return ofx


def fmt_brl(v):
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# ── UI Streamlit ─────────────────────────────────────────────────

st.title("🏦 Conversor de Extrato PDF para OFX")
st.write("Faça o upload do PDF — o banco será detectado automaticamente.")

uploaded_file = st.file_uploader(
    "Selecione o arquivo PDF do extrato", type=["pdf"])

detected_bank = None
preview_lines = []

if uploaded_file is not None:
    try:
        with pdfplumber.open(io.BytesIO(uploaded_file.getvalue())) as pdf:
            for page in pdf.pages[:2]:
                text = page.extract_text()
                if text:
                    preview_lines.extend(text.split("\n"))
        detected_bank = detect_bank(preview_lines)
    except Exception:
        detected_bank = None

col1, col2 = st.columns([2, 1])
with col1:
    bank_options = list(BANK_MAPPING.keys())
    default_index = (
        bank_options.index(detected_bank)
        if detected_bank and detected_bank in bank_options
        else 0
    )
    if uploaded_file is not None:
        if detected_bank:
            st.success(
                f"✅ Banco detectado automaticamente: **{detected_bank}**")
        else:
            st.warning(
                "⚠️ Banco não identificado. Selecione manualmente abaixo.")
    bank_selected = st.selectbox(
        "Leiaute do Banco:", options=bank_options, index=default_index)

with col2:
    manual_initial_balance = st.number_input(
        "Saldo Inicial da Conta (R$):",
        value=0.0, step=100.0, format="%.2f")
    ignorar_contamax = True
    if bank_selected == "Santander (033)":
        ignorar_contamax = st.checkbox(
            "Ignorar resgates/aplicações ContaMax",
            value=True,
            help="Desmarque para o saldo final do OFX bater com o saldo "
                 "da conta corrente (R$ 0,00).")

if uploaded_file is not None:
    if st.button("Converter para OFX e Exibir Extrato", type="primary"):
        parser_func, bank_code = BANK_MAPPING[bank_selected]
        try:
            pdf_bytes = uploaded_file.getvalue()
            text_lines = []
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page in pdf.pages:
                    text = page.extract_text()
                    if text:
                        text_lines.extend(text.split("\n"))

            if bank_code == "033":
                transactions = parser_func(
                    text_lines, pdf_bytes=pdf_bytes,
                    ignorar_contamax=ignorar_contamax)
            else:
                transactions = parser_func(text_lines)

            if not transactions:
                with st.expander(
                        "🔍 Debug — linhas extraídas (primeiras 80)"):
                    for i, l in enumerate(text_lines[:80]):
                        st.text(f"{i:03d}: {l}")
                st.error(
                    "Nenhum lançamento identificado. "
                    "Veja o debug acima.")
            else:
                transactions.sort(key=lambda x: x["date_obj"])

                pdf_initial = BankParsers._extract_initial_balance(
                    text_lines)
                initial_balance = (manual_initial_balance
                                   if manual_initial_balance != 0.0
                                   else pdf_initial)
                total_credits = sum(t["amount"] for t in transactions
                                    if t["amount"] > 0)
                total_debits = sum(t["amount"] for t in transactions
                                   if t["amount"] < 0)
                final_balance = (initial_balance
                                 + total_credits + total_debits)

                st.markdown("---")
                st.subheader("📊 Resumo Financeiro da Conta")
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Saldo Inicial", fmt_brl(initial_balance))
                m2.metric("Entradas (Créditos)", fmt_brl(total_credits))
                m3.metric("Saídas (Débitos)", fmt_brl(abs(total_debits)))
                m4.metric("Saldo Final", fmt_brl(final_balance))

                branch_id, acct_id = BankParsers._extract_account(text_lines)
                ofx_data = generate_ofx(
                    transactions, bank_code,
                    acct_id=acct_id, branch_id=branch_id)
                output_filename = (
                    os.path.splitext(uploaded_file.name)[0] + ".ofx")

                df = pd.DataFrame([{
                    "Data": t["date_obj"].strftime("%d/%m/%Y"),
                    "Descrição": t["description"],
                    "Tipo": "Entrada" if t["amount"] > 0 else "Saída",
                    "Valor (R$)": t["amount"],
                } for t in transactions])

                col_dl1, col_dl2 = st.columns(2)
                with col_dl1:
                    st.download_button(
                        "📥 Baixar OFX",
                        data=ofx_data,
                        file_name=output_filename,
                        mime="application/x-ofx",
                        type="secondary")
                with col_dl2:
                    st.download_button(
                        "📥 Baixar CSV",
                        data=df.to_csv(index=False).encode("utf-8"),
                        file_name=output_filename.replace(".ofx", ".csv"),
                        mime="text/csv",
                        type="secondary")

                st.markdown("---")
                st.subheader(
                    f"📋 Lançamentos — "
                    f"{len(transactions)} registros encontrados")

                def color_amount(val):
                    color = "#28a745" if val > 0 else "#dc3545"
                    return f"color: {color}; font-weight: bold;"

                table_height = min(len(df) * 35 + 38, 800)
                st.dataframe(
                    df.style
                      .map(color_amount, subset=["Valor (R$)"])
                      .format({"Valor (R$)": fmt_brl}),
                    use_container_width=True,
                    height=table_height,
                )

                with st.expander("🔍 Debug — lançamentos capturados"):
                    for t in transactions:
                        st.text(
                            f"{t['date_obj'].strftime('%d/%m/%Y')} | "
                            f"{t['amount']:>12.2f} | "
                            f"{t['description']}")

        except Exception as e:
            st.error(f"Erro ao processar o PDF: {str(e)}")
            st.code(traceback.format_exc())
