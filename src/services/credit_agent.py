"""
Agente de Crédito (CreditAgent)

Este módulo contém a lógica relacionada a consultas de limite e
solicitações de aumento de limite. A implementação usa arquivos CSV
locais como fonte de dados (clientes, tabela de regras por score e
registro de solicitações). O código busca ser robusto a formatos
diferentes de colunas/campos e trata exceções de IO de forma controlada.
"""

from pathlib import Path
from datetime import datetime
import csv
import logging
from typing import Optional, Dict, Any

import pandas as pd

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# --------------------------
# Configuração de caminhos
# --------------------------
# Define diretórios/arquivos de dados relativos ao projeto.
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
CLIENTS_CSV = DATA_DIR / "clientes.csv"
SCORE_LIMIT_CSV = DATA_DIR / "score_limite.csv"
REQUESTS_CSV = DATA_DIR / "solicitacoes_aumento_limite.csv"

REQUESTS_HEADER = [
    "cpf_cliente",
    "data_hora_solicitacao",
    "limite_atual",
    "novo_limite_solicitado",
    "status_pedido"
]


# --------------------------
# Classe principal
# --------------------------
class CreditAgent:
    """
    Classe que encapsula operações do domínio de crédito:
    - leitura de clientes e regras de limite por score,
    - consulta de limite do cliente,
    - solicitação de aumento (com registro em CSV).

    Observação: o uso de pandas facilita leitura flexível do CSV, mas
    todas as escritas (registro de solicitações) são feitas por csv.writer
    para simplicidade e compatibilidade.
    """

    def __init__(self, clients_csv: Optional[Path] = None,
                 score_limit_csv: Optional[Path] = None,
                 requests_csv: Optional[Path] = None):
        # Inicializa caminhos configuráveis (útil para testes)
        self.clients_csv = Path(clients_csv) if clients_csv else CLIENTS_CSV
        self.score_limit_csv = Path(score_limit_csv) if score_limit_csv else SCORE_LIMIT_CSV
        self.requests_csv = Path(requests_csv) if requests_csv else REQUESTS_CSV

        # caches em memória para evitar leituras repetidas
        self._clients_df = None
        self._score_df = None

    # --------------------------
    # Carregamento de dados
    # --------------------------
    # Esses métodos carregam os CSVs quando necessário e normalizam nomes de colunas.
    def _load_clients(self) -> pd.DataFrame:
        """
        Carrega clientes.csv em um DataFrame (cacheado).
        Se houver erro na leitura, retorna DataFrame vazio com colunas mínimas.
        """
        if self._clients_df is None:
            if self.clients_csv.exists():
                try:
                    df = pd.read_csv(self.clients_csv, dtype=str)
                    df.columns = [c.strip().lower() for c in df.columns]
                    self._clients_df = df
                except Exception as e:
                    logger.exception("Erro ao ler clientes.csv: %s", e)
                    self._clients_df = pd.DataFrame(columns=["cpf", "nome", "data_nascimento"])
            else:
                self._clients_df = pd.DataFrame(columns=["cpf", "nome", "data_nascimento"])
        return self._clients_df

    def _load_score_limits(self) -> pd.DataFrame:
        """
        Carrega score_limite.csv em um DataFrame (cacheado).
        Caso o arquivo esteja ausente ou com erro, registra aviso e retorna DataFrame vazio.
        """
        if self._score_df is None:
            if self.score_limit_csv.exists():
                try:
                    df = pd.read_csv(self.score_limit_csv)
                    df.columns = [c.strip().lower() for c in df.columns]
                    self._score_df = df
                except Exception as e:
                    logger.exception("Erro ao ler score_limite.csv: %s", e)
                    self._score_df = pd.DataFrame(columns=["min_score", "max_score", "max_allowed_limit"])
            else:
                logger.warning("Arquivo score_limite.csv não encontrado em %s", self.score_limit_csv)
                self._score_df = pd.DataFrame(columns=["min_score", "max_score", "max_allowed_limit"])
        return self._score_df

    # --------------------------
    # Busca e normalização de cliente
    # --------------------------
    def _find_client_row(self, cpf: str) -> Optional[Dict[str, Any]]:
        """
        Localiza a linha do cliente pelo CPF (apenas dígitos).
        Retorna um dicionário com campos normalizados:
        cpf, nome, data_nascimento, limite_atual, score.
        """
        digits = "".join(ch for ch in (cpf or "") if ch.isdigit())
        if not digits:
            return None
        df = self._load_clients()
        if df.empty:
            return None

        # tenta localizar coluna que contenha 'cpf' no nome
        cpf_cols = [c for c in df.columns if "cpf" in c]
        if not cpf_cols:
            return None
        cpf_col = cpf_cols[0]

        # normaliza coluna de CPF (mantém apenas dígitos) e busca
        df[cpf_col] = df[cpf_col].astype(str).apply(lambda s: "".join(ch for ch in s if ch.isdigit()))
        matched = df[df[cpf_col] == digits]
        if matched.empty:
            return None

        row = matched.iloc[0].to_dict()
        # extrai campos com tolerância a nomes diferentes de coluna
        return {
            "cpf": "".join(ch for ch in str(row.get(cpf_col, "")) if ch.isdigit()),
            "nome": row.get("nome") or row.get("name") or "",
            "data_nascimento": row.get("data_nascimento") or row.get("dob") or "",
            "limite_atual": self._safe_float(row.get("limite_atual") or row.get("limite") or 0.0),
            "score": self._safe_int(row.get("score") or row.get("scoring") or None)
        }

    # --------------------------
    # Utilitários de conversão segura
    # --------------------------
    @staticmethod
    def _safe_float(v):
        """Converte para float, retornando 0.0 em caso de falha."""
        try:
            return float(v)
        except Exception:
            return 0.0

    @staticmethod
    def _safe_int(v):
        """Converte para int de forma segura; retorna None se não for possível."""
        try:
            return int(float(v))
        except Exception:
            return None

    # --------------------------
    # Regras de negócio: limite permitido por score
    # --------------------------
    def _allowed_limit_by_score(self, score: int) -> float:
        """
        Determina o limite permitido com base na tabela score_limite.csv.
        Lê as colunas relevantes (tenta heurísticas se os nomes mudarem)
        e procura a faixa onde score se encaixa.
        """
        df = self._load_score_limits()
        if df.empty or score is None:
            return 0.0

        # tenta identificar colunas por heurística
        min_col = next((c for c in df.columns if "min" in c and "score" in c), None)
        max_col = next((c for c in df.columns if "max" in c and "score" in c), None)
        limit_col = next((c for c in df.columns if "limit" in c or "allowed" in c), None)

        # fallback para os primeiros 3 se heurística não encontrar
        if not (min_col and max_col and limit_col):
            try:
                min_col, max_col, limit_col = df.columns[:3]
            except Exception:
                return 0.0

        try:
            for _, r in df.iterrows():
                try:
                    min_s = int(r[min_col])
                    max_s = int(r[max_col])
                    allowed = float(r[limit_col])
                except Exception:
                    continue
                if min_s <= score <= max_s:
                    return allowed
            return 0.0
        except Exception as e:
            logger.exception("Erro ao interpretar score limits: %s", e)
            return 0.0

    # --------------------------
    # Gerenciamento do arquivo de solicitações
    # --------------------------
    def _ensure_requests_file(self):
        """
        Garante que o diretório de dados existe e que o CSV de solicitações
        possui cabeçalho. Usado antes de gravar novos pedidos.
        """
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not self.requests_csv.exists():
            try:
                with open(self.requests_csv, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(REQUESTS_HEADER)
            except Exception:
                logger.exception("Falha ao criar file %s", self.requests_csv)

    def _append_request(self, cpf_cliente: str, limite_atual: float, novo_limite: float, status: str):
        """
        Registra uma solicitação de aumento no CSV de solicitações.
        Usa timestamp UTC ISO e formata valores com duas casas decimais.
        """
        self._ensure_requests_file()
        timestamp = datetime.utcnow().isoformat()
        try:
            with open(self.requests_csv, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([cpf_cliente, timestamp, f"{limite_atual:.2f}", f"{novo_limite:.2f}", status])
        except Exception as e:
            logger.exception("Erro ao gravar solicitacao: %s", e)
            raise

    # --------------------------
    # API pública do agente
    # --------------------------
    def consulta_limite(self, cpf: str) -> Dict[str, Any]:
        """
        Consulta o limite atual e score do cliente identificado pelo CPF.
        Retorna dicionário com chave 'ok' e detalhes em caso de sucesso.
        """
        try:
            row = self._find_client_row(cpf)
            if not row:
                return {"ok": False, "msg": "Cliente não encontrado.", "cpf": cpf, "nome": "", "limite_atual": 0.0, "score": None}
            return {"ok": True, "cpf": row["cpf"], "nome": row["nome"], "limite_atual": row["limite_atual"], "score": row["score"], "msg": "OK"}
        except Exception as e:
            logger.exception("consulta_limite erro: %s", e)
            return {"ok": False, "msg": "Erro ao consultar limite."}

    def solicitar_aumento(self, cpf: str, novo_limite: float) -> Dict[str, Any]:
        """
        Processo para solicitar aumento de limite:
        - busca dados do cliente;
        - determina score (gera um score heurístico se não houver);
        - compara com o limite permitido pela tabela;
        - registra a solicitação em CSV com status 'aprovado' ou 'rejeitado';
        - retorna detalhes do resultado (reason, status, valores).
        """
        try:
            client = self._find_client_row(cpf)
            if not client:
                return {"ok": False, "msg": "Cliente não encontrado."}

            limite_atual = client.get("limite_atual", 0.0)
            score = client.get("score")

            # se não houver score, gera um valor determinístico simples a partir do CPF
            if score is None:
                digits = "".join(ch for ch in client.get("cpf", "") if ch.isdigit())
                s = sum(int(d) for d in digits) if digits else 0
                score = 300 + (s % 551)

            allowed = self._allowed_limit_by_score(int(score))

            if novo_limite <= allowed:
                status = "aprovado"
                reason = f"Score {score} suficiente para o limite solicitado (permitido até {allowed:.2f})."
            else:
                status = "rejeitado"
                reason = f"Score {score} insuficiente para o limite solicitado (permitido até {allowed:.2f})."

            # registra a solicitação (mesmo em caso de rejeição)
            self._append_request(client["cpf"], limite_atual, novo_limite, status)

            return {
                "ok": True,
                "cpf": client["cpf"],
                "nome": client["nome"],
                "limite_atual": limite_atual,
                "novo_limite_solicitado": novo_limite,
                "status_pedido": status,
                "reason": reason
            }
        except Exception as e:
            logger.exception("Erro ao processar solicitacao: %s", e)
            return {"ok": False, "msg": "Erro interno ao processar solicitação."}
