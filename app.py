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
from ofxparse import OfxParser

# Configuração da página do Streamlit
st.set_page_config(
    page_title="Conversor e Categorizador Extrato OFX",
    page_icon="📊",
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
# 3. SERVIÇOS DE CATEGORIZAÇÃO E LOG
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
<CURDEF>BRL>
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
    st.title("📊 Processador e Categorizador de Extratos OFX")
    st.markdown("Faça o upload dos seus arquivos **.OFX**, categorize transações automaticamente e extraia relatórios consolidados.")

    # Inicialização de diretórios locais temporários
    Path("ofxs_gerados").mkdir(exist_ok=True)
    Path("csv_reports").mkdir(exist_ok=True)

    logger = SimpleLogger()
    categorizer = SmartKeywordCategorizer(DEFAULT_KEYWORDS)

    # Sidebar para controles
    st.sidebar.header("Painel de Controle")
    uploaded_files = st.file_uploader("Selecione arquivos OFX para processar", type=["ofx"], accept_multiple_files=True)

    if uploaded_files:
        st.success(f"{len(uploaded_files)} arquivo(s) carregado(s) com sucesso!")
        
        if st.button("🚀 Processar e Categorizar Extratos"):
            all_transactions = []
            
            for uploaded_file in uploaded_files:
                file_path = Path("ofxs_gerados") / uploaded_file.name
                with open(file_path, "wb") as f:
                    f.write(uploaded_file.getbuffer())
                
                # Processamento do OFX usando ofxparse
                try:
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        ofx = OfxParser()
                        parsed_ofx = ofx.parse(f)
                        
                    for account in parsed_ofx.accounts:
                        if hasattr(account, 'statement') and account.statement:
                            for t in account.statement.transactions:
                                date_str = t.date.strftime('%Y%m%d') if t.date else datetime.now().strftime('%Y%m%d')
                                amount = float(t.amount) if t.amount else 0.0
                                desc = t.memo or t.type or 'Sem descrição'
                                trntype = t.type or ('CREDIT' if amount > 0 else 'DEBIT')
                                
                                category = categorizer.categorize_transaction(desc, amount)
                                
                                all_transactions.append(Transaction(
                                    date=date_str,
                                    amount=amount,
                                    description=desc,
                                    transaction_type="entrada" if amount > 0 else "saída",
                                    trntype=trntype,
                                    category=category,
                                    fitid=t.id if hasattr(t, 'id') and t.id else f"t_{abs(hash(desc))}"
                                ))
                except Exception as e:
                    st.error(f"Erro ao processar o arquivo {uploaded_file.name}: {e}")

            if all_transactions:
                st.session_state['transactions'] = all_transactions
                st.success("Processamento concluído com sucesso!")

    # Exibição de Dados e Relatórios se existirem transações processadas
    if 'transactions' in st.session_state:
        transactions: List[Transaction] = st.session_state['transactions']
        
        st.subheader("📋 Resumo das Transações Processadas")
        
        # Converter para formato tabular para exibição
        data_rows = []
        for tx in transactions:
            data_rows.append({
                "Data": tx.date,
                "Descrição": tx.description,
                "Valor (R$)": tx.amount,
                "Tipo": tx.transaction_type,
                "Categoria": tx.category
            })
            
        import pandas as pd
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

        # Opção de exportação para CSV (incluindo transações "Outros")
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
