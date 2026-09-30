import streamlit as st
import os
import csv
import re
import json
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from abc import ABC, abstractmethod
from dataclasses import dataclass
import pdfplumber
import pandas as pd

# Configuração da página do Streamlit
st.set_page_config(
    page_title="Conversor de Extrato PDF para OFX & Categorizador",
    page_icon="📄",
    layout="wide"
)

# ==========================================
# 1. CLASSES E INTERFACES (Models)
# ==========================================

@dataclass
class Transaction:
    date: str
    amount: float
    description: str
    transaction_type: str
    trntype: str
    category: str = ""
    fitid: str = ""

@dataclass
class AccountData:
    bank_name: str
    agency: str
    account: str
    bank_id: str
    org: str
    fid: str

# ==========================================
# 2. CONFIGURAÇÕES DE PALAVRAS-CHAVE
# ==========================================

DEFAULT_KEYWORDS = {
    "Alimentação": [
        "ifood", "rappi", "uber eats", "mcdonalds", "burger king", "subway", 
        "starbucks", "padaria", "restaurante", "lanchonete", "pizzaria", 
        "supermercado", "extra", "carrefour", "pão de açúcar", "assai", "atacadão"
    ],
    "Transporte": [
        "uber", "99", "taxi", "metrô", "ônibus", "combustível", "gasolina", 
        "etanol", "posto", "shell", "petrobras", "ipiranga", "pedágio"
    ],
    "Saúde": [
        "farmácia", "drogaria", "drogasil", "raia", "unimed", "amil", 
        "hospital", "clínica", "médico", "dentista", "laboratório", "exame"
    ],
    "Moradia": [
        "aluguel", "condomínio", "iptu", "energia", "luz", "enel", 
        "água", "sabesp", "gás", "comgás", "internet", "claro", "vivo", "tim"
    ],
    "Educação": [
        "escola", "colégio", "universidade", "faculdade", "curso", "livro"
    ],
    "Lazer": [
        "cinema", "teatro", "show", "academia", "smart fit", "viagem", "hotel", "airbnb"
    ],
    "Salário": [
        "salário", "salario", "remuneração", "ordenado", "férias", "comissão", "plr"
    ],
    "Transferências": [
        "pix transf", "pix receb", "transferência", "ted", "doc", "pix pago"
    ],
    "Impostos": [
        "ipva", "licenciamento", "imposto", "taxa"
    ]
}

# ==========================================
# 3. SERVIÇOS DE CATEGORIZAÇÃO E PARSER PDF
# ==========================================

class SimpleLogger:
    def info(self, msg): st.info(msg)
    def warning(self, msg): st.warning(msg)
    def error(self, msg): st.error(msg)

class SmartKeywordCategorizer:
    def __init__(self, keywords: Dict[str, List[str]]):
        self.keywords = keywords
        
    def _clean_description(self, description: str) -> str:
        if not description:
            return ""
        return description.lower().strip()
        
    def categorize_transaction(self, description: str, amount: float) -> str:
        cleaned_desc = self._clean_description(description)
        
        for category, kws in self.keywords.items():
            for kw in kws:
                if kw in cleaned_desc:
                    return category
                    
        return "Outros"

    def get_available_categories(self) -> List[str]:
        return list(self.keywords.keys()) + ["Outros"]

class PDFStatementParser:
    """
    Extrai transações de extratos bancários em PDF baseando-se em padrões comuns de linhas 
    (Data, Descrição e Valor monetário).
    """
    @staticmethod
    def parse_pdf(file_path: Path) -> List[Transaction]:
        transactions = []
        current_year = datetime.now().year
        
        # Regex flexível para capturar: Data (DD/MM ou DD/MM/AAAA), Descrição e Valor (ex: 1.234,56 ou -150,00)
        # Exemplo de linha: 15/08/2026 PIX TRANSF JOAO SILVA -150,00 ou 15/08 SUPERMERCADO 45,90
        line_pattern = re.compile(
            r'^(\d{2}/\d{2}(?:/\d{4})?)\s+(.*?)\s+(-?\d{1,3}(?:\.\d{3})*,\d{2})\s*$',
            re.UNICODE
        )
        
        # Padrão alternativo caso o valor venha sem ponto de milhar
        line_pattern_alt = re.compile(
            r'^(\d{2}/\d{2}(?:/\d{4})?)\s+(.*?)\s+(-?\d+,\d{2})\s*$',
            re.UNICODE
        )

        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if not text:
                    continue
                
                lines = text.split('\n')
                for line in lines:
                    line = line.strip()
                    match = line_pattern.match(line) or line_pattern_alt.match(line)
                    
                    if match:
                        date_raw, desc_raw, amount_raw = match.groups()
                        
                        # Processamento da Data
                        try:
                            if len(date_raw.split('/')) == 2:
                                date_str_full = f"{date_raw}/{current_year}"
                                dt_obj = datetime.strptime(date_str_full, '%d/%m/%Y')
                            else:
                                dt_obj = datetime.strptime(date_raw, '%d/%m/%Y')
                            formatted_date = dt_obj.strftime('%Y%m%d')
                        except ValueError:
                            formatted_date = datetime.now().strftime('%Y%m%d')

                        # Processamento do Valor
                        try:
                            clean_amount = amount_raw.replace('.', '').replace(',', '.')
                            amount = float(clean_amount)
                        except ValueError:
                            amount = 0.0

                        description = desc_raw.strip()
                        trntype = 'CREDIT' if amount > 0 else 'DEBIT'
                        
                        transactions.append(Transaction(
                            date=formatted_date,
                            amount=amount,
                            description=description,
                            transaction_type="entrada" if amount > 0 else "saída",
                            trntype=trntype,
                            fitid=f"pdf_{abs(hash(f'{formatted_date}_{description}_{amount}'))}"
                        ))
                        
        return transactions

# ==========================================
# 4. PROCESSADORES E ESCRITORES OFX
# ==========================================

class OFXWriterRefactored:
    def write(self, transactions: List[Transaction], account_data: AccountData) -> str:
        ofx_content = f"""OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE

<OFX>
<SIGNONMSGSRSV1>
<SONRS>
<STATUS>
<CODE>0
<SEVERITY>INFO
</STATUS>
<DTSERVER>{datetime.now().strftime('%Y%m%d%H%M%S')}</DTSERVER>
<LANGUAGE>POR</LANGUAGE>
</SONRS>
</SIGNONMSGSRSV1>
<BANKMSGSRSV1>
<STMTTRNRS>
<TRNUID>1
<STATUS>
<CODE>0
<SEVERITY>INFO
</STATUS>
<STMTRS>
<CURDEF>BRL</CURDEF>
<BANKACCTFROM>
<BANKID>{account_data.bank_id}</BANKID>
<ACCTID>{account_data.account}</ACCTID>
<ACCTTYPE>CHECKING</ACCTTYPE>
</BANKACCTFROM>
<BANKTRANLIST>
"""
        for i, tx in enumerate(transactions):
            fitid = tx.fitid or f"trans_{i+1:03d}_{tx.date}"
            ofx_content += f"""<STMTTRN>
<TRNTYPE>{tx.trntype}</TRNTYPE>
<DTPOSTED>{tx.date}</DTPOSTED>
<TRNAMT>{tx.amount:.2f}</TRNAMT>
<FITID>{fitid}</FITID>
<MEMO>{tx.description} [Cat: {tx.category}]</MEMO>
</STMTTRN>
"""

        ofx_content += """</BANKTRANLIST>
<LEDGERBAL>
<BALAMT>0.00</BALAMT>
<DTASOF>{}</DTASOF>
</LEDGERBAL>
</STMTRS>
</STMTTRNRS>
</BANKMSGSRSV1>
</OFX>
""".format(datetime.now().strftime('%Y%m%d%H%M%S'))
        return ofx_content

# ==========================================
# 5. APLICAÇÃO STREAMLIT PRINCIPAL
# ==========================================

def main():
    st.title("📄 Conversor de Extrato PDF para OFX & Categorizador")
    st.markdown("Faça o upload dos seus extratos em **PDF**, extraia as transações automaticamente, categorize-as e gere o arquivo **OFX** compatível com sistemas contábeis e financeiros.")

    # Inicialização de diretórios locais temporários
    Path("pdf_uploads").mkdir(exist_ok=True)
    Path("ofxs_gerados").mkdir(exist_ok=True)
    Path("csv_reports").mkdir(exist_ok=True)

    logger = SimpleLogger()
    categorizer = SmartKeywordCategorizer(DEFAULT_KEYWORDS)

    # Sidebar para controles e Dados da Conta para o OFX
    st.sidebar.header("⚙️ Configurações da Conta")
    bank_id = st.sidebar.text_input("Código do Banco (Ex: 001, 341)", "001")
    agency = st.sidebar.text_input("Agência", "1234")
    account = st.sidebar.text_input("Conta Corrente", "56789-0")

    st.sidebar.markdown("---")
    uploaded_files = st.file_uploader("Selecione arquivos PDF de extratos para processar", type=["pdf"], accept_multiple_files=True)

    if uploaded_files:
        st.success(f"{len(uploaded_files)} arquivo(s) PDF carregado(s) com sucesso!")
        
        if st.button("🚀 Extrair, Categorizar e Gerar OFX"):
            all_transactions = []
            
            for uploaded_file in uploaded_files:
                file_path = Path("pdf_uploads") / uploaded_file.name
                with open(file_path, "wb") as f:
                    f.write(uploaded_file.getbuffer())
                
                try:
                    # Extração do PDF via PDFStatementParser
                    extracted_txs = PDFStatementParser.parse_pdf(file_path)
                    
                    for t in extracted_txs:
                        category = categorizer.categorize_transaction(t.description, t.amount)
                        t.category = category
                        all_transactions.append(t)
                        
                except Exception as e:
                    st.error(f"Erro ao processar o PDF {uploaded_file.name}: {e}")

            if all_transactions:
                st.session_state['transactions'] = all_transactions
                st.session_state['account_data'] = AccountData(
                    bank_name="Banco",
                    agency=agency,
                    account=account,
                    bank_id=bank_id,
                    org="",
                    fid=""
                )
                st.success(f"Processamento concluído! {len(all_transactions)} transações extraídas com sucesso.")
            else:
                st.warning("Nenhuma transação pôde ser identificada automaticamente com o padrão atual do PDF. Verifique se o formato do texto do extrato é compatível.")

    # Exibição de Dados e Relatórios se existirem transações processadas
    if 'transactions' in st.session_state:
        transactions: List[Transaction] = st.session_state['transactions']
        
        st.subheader("📋 Resumo das Transações Extraídas")
        
        data_rows = []
        for tx in transactions:
            data_rows.append({
                "Data": tx.date,
                "Descrição": tx.description,
                "Valor (R$)": tx.amount,
                "Tipo": tx.transaction_type,
                "Categoria": tx.category
            })
            
        df = pd.DataFrame(data_rows)
        
        # Filtro por Categoria na interface
        selected_category = st.selectbox("Filtrar por Categoria", ["Todas"] + list(categorizer.get_available_categories()))
        if selected_category != "Todas":
            filtered_df = df[df["Categoria"] == selected_category]
        else:
            filtered_df = df
            
        st.dataframe(filtered_df, use_container_width=True)
        
        # Métricas rápidas
        total_receitas = df[df["Valor (R$)"] > 0]["Valor (R$)"].sum()
        total_despesas = df[df["Valor (R$)"] < 0]["Valor (R$)"].sum()
        
        col1, col2, col3 = st.columns(3)
        col1.metric("Total de Transações", len(df))
        col2.metric("Entradas Totais", f"R$ {total_receitas:,.2f}")
        col3.metric("Saídas Totais", f"R$ {total_despesas:,.2f}")

        # Geração do Arquivo OFX para Download
        st.subheader("💾 Geração de Arquivo OFX")
        writer = OFXWriterRefactored()
        account_data = st.session_state['account_data']
        ofx_string = writer.write(transactions, account_data)
        
        st.download_button(
            label="Baixar Arquivo OFX Convertido",
            data=ofx_string,
            file_name="extrato_convertido.ofx",
            mime="application/x-ofx"
        )

        # Opção de exportação para CSV
        st.subheader("📥 Exportação de Relatórios")
        csv_data = df.to_csv(index=False).encode('utf-8')
        st.download_button(
            label="Baixar Relatório Completo em CSV",
            data=csv_data,
            file_name="extrato_categorizado.csv",
            mime="text/csv"
        )
        
        # Filtro específico para transações "Outros"
        outros_df = df[df["Categoria"] == "Outros"]
        if not outros_df.empty:
            st.warning(f"Existem {len(outros_df)} transações classificadas como 'Outros'.")
            outros_csv = outros_df.to_csv(index=False).encode('utf-8')
            st.download_button(
                label="Baixar Transações 'Outros' para Revisão",
                data=outros_csv,
                file_name="transacoes_outros.csv",
                mime="text/csv"
            )

if __name__ == '__main__':
    main()
