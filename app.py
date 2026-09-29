import os
import re
from datetime import datetime
import io
import streamlit as st
import pdfplumber
import pandas as pd

# Configuração da página no Streamlit
st.set_page_config(
    page_title="Conversor Extrato PDF para OFX",
    page_icon="🏦",
    layout="wide"
)

# ==========================================
# 1. PARSERS ESPECÍFICOS POR BANCO
# ==========================================

class BankParsers:
    @staticmethod
    def _parse_date(dt_str):
        current_year = datetime.now().year
        parts = dt_str.split("/")
        if len(parts) == 2:
            dt_formatted = f"{parts[0]}/{parts[1]}/{current_year}"
        elif len(parts) == 3 and len(parts[2]) == 2:
            dt_formatted = f"{parts[0]}/{parts[1]}/20{parts[2]}"
        else:
            dt_formatted = dt_str
            
        try:
            return datetime.strptime(dt_formatted, "%d/%m/%Y")
        except ValueError:
            return None

    @staticmethod
    def _extract_initial_balance(text_lines):
        """Busca por palavras-chave comuns de saldo anterior/inicial no texto do PDF."""
        pattern_saldo = re.compile(r"(?:SALDO\s+ANTERIOR|SD\s+CTA/APL|SALDO\s+INICIAL)\s*[:\.-]?\s*(-?[\d\.]+\,\d{2})", re.IGNORECASE)
        for line in text_lines:
            match = pattern_saldo.search(line.strip())
            if match:
                try:
                    val_str = match.group(1)
                    return float(val_str.replace(".", "").replace(",", "."))
                except ValueError:
                    continue
        return 0.0

    @staticmethod
    def itau(text_lines):
        transactions = []
        pattern = re.compile(
            r"(\d{2}/\d{2}(?:/\d{2,4})?)\s+(.+?)\s+(-?[\d\.]+\,\d{2})\s*([CD])?", 
            re.IGNORECASE
        )
        for line in text_lines:
            line_str = line.strip()
            if any(term in line_str.upper() for term in ["SALDO DA CONTA", "SD CTA/APL", "SALDO ANTERIOR"]):
                continue
                
            match = pattern.search(line_str)
            if match:
                dt_str, desc, val_str, tp = match.groups()
                dt_obj = BankParsers._parse_date(dt_str)
                if not dt_obj:
                    continue

                val = float(val_str.replace(".", "").replace(",", "."))
                if tp:
                    if tp.upper() == "D":
                        val = -abs(val)
                    elif tp.upper() == "C":
                        val = abs(val)

                transactions.append({"date_obj": dt_obj, "amount": val, "description": desc.strip()})
        return transactions

    @staticmethod
    def banco_do_brasil(text_lines):
        transactions = []
        pattern = re.compile(
            r"(\d{2}/\d{2}(?:/\d{2,4})?)\s+(.+?)\s+([\d\.]+\,\d{2})\s*([CD])", 
            re.IGNORECASE
        )
        for line in text_lines:
            line_str = line.strip()
            if any(term in line_str.upper() for term in ["SALDO ANTERIOR", "S A L D O", "RESUMO"]):
                continue

            match = pattern.search(line_str)
            if match:
                dt_str, desc, val_str, tp = match.groups()
                dt_obj = BankParsers._parse_date(dt_str)
                if not dt_obj:
                    continue

                val = float(val_str.replace(".", "").replace(",", "."))
                if tp.upper() == "D":
                    val = -abs(val)
                transactions.append({"date_obj": dt_obj, "amount": val, "description": desc.strip()})
        return transactions

    @staticmethod
    def bradesco(text_lines):
        transactions = []
        pattern = re.compile(
            r"(\d{2}/\d{2}(?:/\d{2,4})?)\s+(.+?)\s+(-?[\d\.]+\,\d{2})([\+-])?", 
            re.IGNORECASE
        )
        for line in text_lines:
            line_str = line.strip()
            if any(term in line_str.upper() for term in ["SALDO ANTERIOR", "ULTIMO SALDO"]):
                continue

            match = pattern.search(line_str)
            if match:
                dt_str, desc, val_str, signal = match.groups()
                dt_obj = BankParsers._parse_date(dt_str)
                if not dt_obj:
                    continue

                val = float(val_str.replace(".", "").replace(",", "."))
                if signal == "-" or val_str.startswith("-"):
                    val = -abs(val)
                transactions.append({"date_obj": dt_obj, "amount": val, "description": desc.strip()})
        return transactions

    @staticmethod
    def santander(text_lines):
        transactions = []

        # ============================================================
        # CORREÇÃO: pré-processamento para reunir linhas quebradas
        # O PDF do Santander quebra o histórico em múltiplas linhas.
        # Ex: "09/03/2026 PIX ENVIADO VALDEIR AZARIAS VAL"
        #     "ENTIN 370992 -7.000,00"
        # A lógica abaixo detecta se uma linha NÃO começa com uma data
        # e a concatena à linha anterior, reconstituindo o lançamento
        # completo antes de aplicar a regex.
        # ============================================================
        date_pattern = re.compile(r"^\d{2}/\d{2}(?:/\d{2,4})?")
        merged_lines = []
        for line in text_lines:
            line_str = line.strip()
            if not line_str:
                continue
            if date_pattern.match(line_str):
                # Linha começa com data → novo lançamento
                merged_lines.append(line_str)
            else:
                # Linha NÃO começa com data → continuação da linha anterior
                if merged_lines:
                    merged_lines[-1] = merged_lines[-1] + " " + line_str
                else:
                    merged_lines.append(line_str)
        # ============================================================
        # FIM DA CORREÇÃO — daqui para baixo é idêntico ao original
        # ============================================================

        pattern = re.compile(
            r"(\d{2}/\d{2}(?:/\d{2,4})?)\s+(.+?)\s+(-?[\d\.]+\,\d{2})\s*([CD])?", 
            re.IGNORECASE
        )
        # CORREÇÃO: itera sobre merged_lines em vez de text_lines
        for line in merged_lines:
            line_str = line.strip()
            if any(term in line_str.upper() for term in ["SALDO ANTERIOR", "TOTAL"]):
                continue

            match = pattern.search(line_str)
            if match:
                dt_str, desc, val_str, tp = match.groups()
                dt_obj = BankParsers._parse_date(dt_str)
                if not dt_obj:
                    continue

                val = float(val_str.replace(".", "").replace(",", "."))
                if tp and tp.upper() == "D":
                    val = -abs(val)
                transactions.append({"date_obj": dt_obj, "amount": val, "description": desc.strip()})
        return transactions

    @staticmethod
    def caixas(text_lines):
        transactions = []
        pattern = re.compile(
            r"(\d{2}/\d{2}(?:/\d{2,4})?)\s*(?:\d+)?\s+(.+?)\s+([\d\.]+\,\d{2})\s*([CD])", 
            re.IGNORECASE
        )
        for line in text_lines:
            line_str = line.strip()
            if any(term in line_str.upper() for term in ["SALDO ANTER", "SALDO DIA"]):
                continue

            match = pattern.search(line_str)
            if match:
                dt_str, desc, val_str, tp = match.groups()
                dt_obj = BankParsers._parse_date(dt_str)
                if not dt_obj:
                    continue

                val = float(val_str.replace(".", "").replace(",", "."))
                if tp.upper() == "D":
                    val = -abs(val)
                transactions.append({"date_obj": dt_obj, "amount": val, "description": desc.strip()})
        return transactions

    @staticmethod
    def generic_fallback(text_lines):
        transactions = []
        pattern = re.compile(
            r"(\d{2}/\d{2}(?:/\d{2,4})?)\s+(.+?)\s+(-?[\d\.]+\,\d{2})\s*([CD])?", 
            re.IGNORECASE
        )
        for line in text_lines:
            line_str = line.strip()
            if any(term in line_str.upper() for term in ["SALDO ANTERIOR", "RENDIMENTO", "TOTAL"]):
                continue

            match = pattern.search(line_str)
            if match:
                dt_str, desc, val_str, tp = match.groups()
                dt_obj = BankParsers._parse_date(dt_str)
                if not dt_obj:
                    continue

                val = float(val_str.replace(".", "").replace(",", "."))
                if tp and tp.upper() == "D":
                    val = -abs(val)

                transactions.append({"date_obj": dt_obj, "amount": val, "description": desc.strip()})
        return transactions


BANK_MAPPING = {
    "Itaú Unibanco (341)": (BankParsers.itau, "341"),
    "Bradesco (237)": (BankParsers.bradesco, "237"),
    "Santander (033)": (BankParsers.santander, "033"),
    "Banco do Brasil (001)": (BankParsers.banco_do_brasil, "001"),
    "Caixa Econômica Federal (104)": (BankParsers.caixas, "104"),
    "Sicoob (756)": (BankParsers.generic_fallback, "756"),
    "Sicredi (748)": (BankParsers.generic_fallback, "748"),
    "Banco Inter (077)": (BankParsers.generic_fallback, "077"),
    "Nubank (260)": (BankParsers.generic_fallback, "260"),
    "C6 Bank (336)": (BankParsers.generic_fallback, "336"),
    "Banrisul (041)": (BankParsers.generic_fallback, "041"),
    "Stone Pagamentos (197)": (BankParsers.generic_fallback, "197"),
    "Unicred (136)": (BankParsers.generic_fallback, "136"),
    "Mercado Pago (323)": (BankParsers.generic_fallback, "323")
}

# ==========================================
# 2. GERADOR DE ESTRUTURA OFX
# ==========================================

def generate_ofx(transactions, bank_code="000"):
    now = datetime.now().strftime("%Y%m%d%H%M%S")
    dt_start = transactions[0]['date_obj'].strftime("%Y%m%d") if transactions else now
    dt_end = transactions[-1]['date_obj'].strftime("%Y%m%d") if transactions else now

    ofx_content = f"""OFXHEADER:100
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
<ACCTID>00000000</ACCTID>
<ACCTTYPE>CHECKING</ACCTTYPE>
</BANKACCTFROM>
<BANKTRANLIST>
<DTSTART>{dt_start}</DTSTART>
<DTEND>{dt_end}</DTEND>
"""

    for idx, tr in enumerate(transactions):
        tr_type = "CREDIT" if tr["amount"] > 0 else "DEBIT"
        dt_str = tr['date_obj'].strftime("%Y%m%d")
        ofx_content += f"""<STMTTRN>
<TRNTYPE>{tr_type}</TRNTYPE>
<DTPOSTED>{dt_str}</DTPOSTED>
<TRNAMT>{tr['amount']:.2f}</TRNAMT>
<FITID>{dt_str}{idx+1:04d}</FITID>
<MEMO>{tr['description']}</MEMO>
</STMTTRN>
"""

    ofx_content += """</BANKTRANLIST>
</STMTRS>
</STMTTRNRS>
</BANKMSGSRSV1>
</OFX>"""

    return ofx_content

# ==========================================
# 3. INTERFACE WEB (STREAMLIT)
# ==========================================

st.title("🏦 Conversor de Extrato PDF para OFX")
st.write("Selecione o banco, faça o upload do PDF e visualize o resumo financeiro com saldos e lançamentos formatados.")

col_config1, col_config2 = st.columns([2, 1])

with col_config1:
    bank_selected = st.selectbox("Selecione o Leiaute do Banco:", options=list(BANK_MAPPING.keys()))

with col_config2:
    manual_initial_balance = st.number_input(
        "Saldo Inicial da Conta (R$):", 
        value=0.0, 
        step=100.0, 
        format="%.2f",
        help="Caso o PDF não informe o saldo anterior, você pode digitar o saldo inicial para conciliação exata."
    )

uploaded_file = st.file_uploader("Selecione o arquivo PDF do extrato", type=["pdf"])

if uploaded_file is not None:
    if st.button("Converter para OFX e Exibir Extrato", type="primary"):
        parser_func, bank_code = BANK_MAPPING[bank_selected]
        
        try:
            text_lines = []
            with pdfplumber.open(io.BytesIO(uploaded_file.read())) as pdf:
                for page in pdf.pages:
                    text = page.extract_text()
                    if text:
                        text_lines.extend(text.split("\n"))

            transactions = parser_func(text_lines)

            if not transactions:
                st.error(f"Nenhum lançamento foi identificado com o leiaute do banco selecionado ({bank_selected}). Verifique se o arquivo PDF contém texto selecionável.")
            else:
                transactions.sort(key=lambda x: x["date_obj"])

                pdf_initial_balance = BankParsers._extract_initial_balance(text_lines)
                initial_balance = manual_initial_balance if manual_initial_balance != 0.0 else pdf_initial_balance

                total_credits = sum(tr["amount"] for tr in transactions if tr["amount"] > 0)
                total_debits = sum(tr["amount"] for tr in transactions if tr["amount"] < 0)
                final_balance = initial_balance + total_credits + total_debits

                st.markdown("---")
                st.subheader("📊 Resumo Financeiro da Conta")
                
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Saldo Inicial", f"R$ {initial_balance:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
                m2.metric("Entradas (Créditos)", f"R$ {total_credits:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."), delta_color="normal")
                m3.metric("Saídas (Débitos)", f"R$ {abs(total_debits):,.2f}".replace(",", "X").replace(".", ",").replace("X", "."), delta_color="inverse")
                m4.metric("Saldo Final", f"R$ {final_balance:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))

                ofx_data = generate_ofx(transactions, bank_code)
                output_filename = os.path.splitext(uploaded_file.name)[0] + ".ofx"
                
                st.download_button(
                    label="📥 Baixar Arquivo OFX Gerado",
                    data=ofx_data,
                    file_name=output_filename,
                    mime="application/x-ofx",
                    type="secondary"
                )

                st.markdown("---")
                st.subheader("📋 Lançamentos Extrato (Ordem Cronológica)")

                df = pd.DataFrame([
                    {
                        "Data": tr["date_obj"].strftime("%d/%m/%Y"),
                        "Descrição": tr["description"],
                        "Tipo": "Entrada" if tr["amount"] > 0 else "Saída",
                        "Valor (R$)": tr["amount"]
                    }
                    for tr in transactions
                ])

                def color_amount(val):
                    color = "#28a745" if val > 0 else "#dc3545"
                    return f"color: {color}; font-weight: bold;"

                styled_df = df.style.map(color_amount, subset=["Valor (R$)"]).format({
                    "Valor (R$)": lambda x: f"R$ {x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                })

                st.dataframe(styled_df, use_container_width=True, height=400)

        except Exception as e:
            st.error(f"Ocorreu um erro ao processar o arquivo PDF: {str(e)}")
