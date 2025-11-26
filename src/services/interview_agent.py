# src/services/interview_agent.py
"""
Agente de Entrevista de Crédito (stateful)

Este módulo implementa um agente conversacional de entrevista financeira,
responsável por:
- conduzir perguntas estruturadas (renda, emprego, despesas, dependentes, dívidas);
- calcular um novo score com base em pesos pré-definidos;
- atualizar o score do cliente no arquivo clientes.csv;
- sinalizar para o fluxo de triagem/crédito que a entrevista terminou
  e que o cliente pode ser reavaliado.

Toda a lógica é feita localmente, sem envio de PII para serviços externos.
"""

from typing import Dict, Any, Optional
import csv
import os
import tempfile
import shutil
from datetime import datetime
import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class InterviewAgent:
    """
    Classe responsável por gerenciar o fluxo da entrevista de crédito.
    Ela mantém estado interno (pergunta atual, respostas, tentativas),
    além de cuidar da leitura e escrita no CSV de clientes.
    """

    def __init__(self, clients_csv_path: str):
        # Caminho do arquivo de clientes que será lido/atualizado
        self.clients_csv = clients_csv_path

        # Estado da conversa (máquina de estados simples)
        self.state = "idle"
        self.cpf = None
        self.client_row = None  # linha do cliente no CSV (dict)

        # Armazena as respostas da entrevista, chaveadas pelas variáveis de interesse
        self.answers = {
            "renda_mensal": None,
            "tipo_emprego": None,
            "despesas_fixas": None,
            "dependentes": None,
            "tem_dividas": None
        }

        # Contadores de tentativas por pergunta (para evitar loop infinito)
        self.retries = {k: 0 for k in self.answers.keys()}
        self.max_retries = 2

        # Pesos usados no cálculo do score, conforme o enunciado do desafio
        self.peso_renda = 30.0
        self.peso_emprego = {
            "formal": 300.0,
            "autônomo": 200.0,
            "autonomo": 200.0,
            "desempregado": 0.0
        }
        self.peso_dependentes = {0: 100.0, 1: 80.0, 2: 60.0, "3+": 30.0}
        self.peso_dividas = {"sim": -100.0, "não": 100.0, "nao": 100.0}

    # ----------------- helpers CSV -----------------
    # Nesta seção ficam as rotinas de leitura e escrita do CSV de clientes.
    def _read_clients(self) -> list:
        """
        Lê o arquivo de clientes e retorna uma lista de dicionários (DictReader).
        Se o arquivo não existir, retorna lista vazia.
        """
        if not os.path.exists(self.clients_csv):
            return []
        with open(self.clients_csv, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            return list(reader)

    def _write_clients_atomic(self, rows: list, fieldnames: list):
        """
        Escreve o CSV de clientes de forma atômica, usando um arquivo temporário.
        Isso evita corromper o arquivo caso aconteça algum erro no meio da escrita.
        """
        dirpath = os.path.dirname(self.clients_csv) or "."
        fd, tmp_path = tempfile.mkstemp(dir=dirpath, prefix="clients_tmp_", text=True)
        os.close(fd)
        try:
            with open(tmp_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for r in rows:
                    writer.writerow(r)
            shutil.move(tmp_path, self.clients_csv)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    def _find_client_row(self, cpf: str) -> Optional[Dict[str, str]]:
        """
        Procura a linha do cliente no CSV pelo CPF (comparando apenas dígitos).
        Retorna o dict da linha encontrada, ou None se não existir.
        """
        rows = self._read_clients()
        for r in rows:
            rcpf = "".join([c for c in (r.get("cpf") or "") if c.isdigit()])
            if rcpf == "".join([c for c in (cpf or "") if c.isdigit()]):
                return r
        return None

    def _update_client_score_in_csv(self, cpf: str, new_score: float) -> bool:
        """
        Atualiza o campo de score do cliente no CSV.  
        - Garante que a coluna 'score' exista (cria se não existir).
        - Atualiza 'score_updated_at' com o timestamp da alteração.
        - Retorna True se encontrou e atualizou o cliente, False caso contrário.
        """
        rows = self._read_clients()
        if not rows:
            logger.warning("clientes.csv vazio ou inexistente ao tentar atualizar score.")
            return False

        fieldnames = list(rows[0].keys())

        # Garante que a coluna 'score' exista para todas as linhas
        if "score" not in fieldnames:
            fieldnames.append("score")
            for r in rows:
                r.setdefault("score", "")

        updated = False
        for r in rows:
            rcpf = "".join([c for c in (r.get("cpf") or "") if c.isdigit()])
            if rcpf == "".join([c for c in (cpf or "") if c.isdigit()]):
                r["score"] = f"{int(round(new_score))}"
                r["score_updated_at"] = datetime.utcnow().isoformat() + "Z"
                updated = True
                break

        if not updated:
            logger.warning("Cliente não encontrado no CSV durante update (CPF=%s).", cpf)
            return False

        # Garante que a coluna de data de atualização também esteja nos fieldnames
        if "score_updated_at" not in fieldnames:
            fieldnames.append("score_updated_at")

        try:
            self._write_clients_atomic(rows, fieldnames)
            return True
        except Exception as e:
            logger.exception("Erro ao gravar clientes.csv: %s", e)
            return False

    # ----------------- interview flow -----------------
    # Aqui mora a máquina de estados da entrevista: start() e handle().
    def start(self, cpf: str) -> str:
        """
        Inicia a entrevista para o CPF informado:
        - valida se o cliente existe;
        - zera respostas e contadores;
        - coloca o estado em 'ask_renda' e devolve a primeira pergunta.
        """
        self.cpf = cpf
        self.client_row = self._find_client_row(cpf)
        if not self.client_row:
            self.state = "idle"
            return ("Não foi possível localizar sua conta com o CPF informado. "
                    "Por favor verifique os dados ou contate o suporte.")

        # reseta respostas, tentativas e estado para novo fluxo
        self.answers = {k: None for k in self.answers.keys()}
        self.retries = {k: 0 for k in self.answers.keys()}
        self.state = "ask_renda"

        return (
            "Ótimo — vou fazer algumas perguntas rápidas para atualizar seu score.\n"
            "Primeira pergunta: qual sua **renda mensal** média (apenas números, ex.: 3500.50)?"
        )

    def handle(self, user_input: str) -> Dict[str, Any]:
        """
        Processa a resposta do usuário de acordo com o estado atual da entrevista.

        Retorno:
        {
          "assistant": str,   # próxima mensagem para o usuário
          "done": bool,       # indica se a entrevista foi concluída
          "redirect": str|None  # exemplo: 'credit' quando deve voltar para o agente de crédito
        }
        """
        if self.state == "idle":
            return {
                "assistant": "Entrevista não iniciada. Chame start(cpf) para iniciar.",
                "done": True
            }

        text = (user_input or "").strip()

        # ---------- Pergunta: renda ----------
        if self.state == "ask_renda":
            try:
                val = float(text.replace(",", "."))
                if val < 0:
                    raise ValueError()
            except Exception:
                self.retries["renda_mensal"] += 1
                if self.retries["renda_mensal"] > self.max_retries:
                    self.state = "idle"
                    return {
                        "assistant": "Não entendi o valor da renda. Vamos encerrar e você pode tentar novamente mais tarde.",
                        "done": True
                    }
                return {
                    "assistant": "Formato inválido. Informe sua renda mensal como número (ex.: 3500.50).",
                    "done": False
                }

            self.answers["renda_mensal"] = float(val)
            self.state = "ask_emprego"
            return {
                "assistant": "Qual o seu tipo de emprego? Responda com: 'formal', 'autônomo' ou 'desempregado'.",
                "done": False
            }

        # ---------- Pergunta: tipo de emprego ----------
        if self.state == "ask_emprego":
            t = text.lower()
            if any(k in t for k in ("formal", "empregado", "clt")):
                tipo = "formal"
            elif any(k in t for k in ("autônomo", "autonomo")):
                tipo = "autônomo"
            elif any(k in t for k in ("desempregado", "sem emprego", "desemprego")):
                tipo = "desempregado"
            else:
                self.retries["tipo_emprego"] += 1
                if self.retries["tipo_emprego"] > self.max_retries:
                    self.state = "idle"
                    return {
                        "assistant": "Não entendi seu tipo de emprego. Encerrando entrevista. Tente novamente depois.",
                        "done": True
                    }
                return {
                    "assistant": "Tipo de emprego inválido. Responda com: 'formal', 'autônomo' ou 'desempregado'.",
                    "done": False
                }

            self.answers["tipo_emprego"] = tipo
            self.state = "ask_despesas"
            return {
                "assistant": "Qual o total aproximado de suas despesas fixas mensais (apenas números, ex.: 1200.00)?",
                "done": False
            }

        # ---------- Pergunta: despesas fixas ----------
        if self.state == "ask_despesas":
            try:
                val = float(text.replace(",", "."))
                if val < 0:
                    raise ValueError()
            except Exception:
                self.retries["despesas_fixas"] += 1
                if self.retries["despesas_fixas"] > self.max_retries:
                    self.state = "idle"
                    return {
                        "assistant": "Formato inválido para despesas. Encerrando entrevista. Tente novamente mais tarde.",
                        "done": True
                    }
                return {
                    "assistant": "Formato inválido. Informe o total das despesas fixas mensais em número (ex.: 1200.00).",
                    "done": False
                }

            self.answers["despesas_fixas"] = float(val)
            self.state = "ask_dependentes"
            return {
                "assistant": "Quantos dependentes você possui? Informe apenas um número (0, 1, 2, 3, ...).",
                "done": False
            }

        # ---------- Pergunta: dependentes ----------
        if self.state == "ask_dependentes":
            try:
                val = int(float(text))  # aceita "0", "1", "2.0" etc.
                if val < 0:
                    raise ValueError()
            except Exception:
                self.retries["dependentes"] += 1
                if self.retries["dependentes"] > self.max_retries:
                    self.state = "idle"
                    return {
                        "assistant": "Não foi possível entender o número de dependentes. Encerrando entrevista.",
                        "done": True
                    }
                return {
                    "assistant": "Informe o número de dependentes usando apenas um número inteiro (ex.: 0, 1, 2).",
                    "done": False
                }

            # Se >= 3, mapeia para a categoria '3+'
            self.answers["dependentes"] = val if val < 3 else "3+"
            self.state = "ask_dividas"
            return {
                "assistant": "Você possui dívidas ativas? Responda 'sim' ou 'não'.",
                "done": False
            }

        # ---------- Pergunta: dívidas ----------
        if self.state == "ask_dividas":
            t = text.lower()
            if any(k in t for k in ("sim", "s", "tenho", "possuo")):
                tem = "sim"
            elif any(k in t for k in ("não", "nao", "n", "não tenho", "nao tenho")):
                tem = "não"
            else:
                self.retries["tem_dividas"] += 1
                if self.retries["tem_dividas"] > self.max_retries:
                    self.state = "idle"
                    return {
                        "assistant": "Resposta inválida para existência de dívidas. Encerrando entrevista.",
                        "done": True
                    }
                return {
                    "assistant": "Não entendi. Você possui dívidas ativas? Responda 'sim' ou 'não'.",
                    "done": False
                }

            self.answers["tem_dividas"] = tem

            # Terminou as perguntas; agora calcula o score com base nas respostas
            try:
                new_score = self._calculate_score()
            except Exception as e:
                logger.exception("Erro ao calcular score: %s", e)
                self.state = "idle"
                return {
                    "assistant": "Erro ao calcular o score. Encerrando. Tente novamente mais tarde.",
                    "done": True
                }

            # Atualiza o CSV com o novo score
            ok = self._update_client_score_in_csv(self.cpf, new_score)
            name = (self.client_row.get("nome") if self.client_row else "cliente")

            if ok:
                self.state = "done"
                return {
                    "assistant": (
                        f"Entrevista finalizada. Seu novo score estimado é {int(round(new_score))} (0-1000). "
                        f"Atualizamos seu registro. Vou encaminhá-lo(a) de volta para análise de crédito."
                    ),
                    "done": True,
                    "redirect": "credit"
                }
            else:
                self.state = "done"
                return {
                    "assistant": (
                        "Entrevista finalizada, porém não foi possível atualizar seu registro no momento. "
                        "Por favor, tente novamente mais tarde ou contate o suporte."
                    ),
                    "done": True,
                    "redirect": "credit"
                }

        # Se chegar aqui, o estado é inválido e não faz sentido continuar
        return {"assistant": "Estado inválido. Encerrando.", "done": True}

    # ---------------- calculation ----------------
    # Esta parte cuida do cálculo numérico do score a partir das respostas.
    def _calculate_score(self) -> float:
        """
        Calcula o score a partir dos pesos definidos.

        Fórmula (base):
        score = (
            (renda_mensal / (despesas + 1)) * peso_renda
            + peso_emprego[tipo_emprego]
            + peso_dependentes[num_dependentes]
            + peso_dividas[tem_dividas]
        )

        No final, o valor é "clampado" para ficar no intervalo 0 a 1000.
        """
        renda = float(self.answers["renda_mensal"] or 0.0)
        despesas = float(self.answers["despesas_fixas"] or 0.0)
        tipo = self.answers["tipo_emprego"] or "desempregado"
        depend = self.answers["dependentes"]
        tem_div = self.answers["tem_dividas"] or "sim"

        # Componente de renda (protege divisão por zero somando 1)
        renda_comp = (renda / (despesas + 1.0)) * self.peso_renda

        # Componente de emprego
        emp_comp = self.peso_emprego.get(tipo, 0.0)

        # Componente de dependentes (inteiro ou categoria "3+")
        if isinstance(depend, int):
            dep_comp = self.peso_dependentes.get(depend, 30.0)
        else:
            dep_comp = self.peso_dependentes.get(depend, self.peso_dependentes["3+"])

        # Componente de dívidas (penaliza ou beneficia conforme o mapa)
        div_comp = self.peso_dividas.get(tem_div, -100.0)

        raw_score = renda_comp + emp_comp + dep_comp + div_comp

        # Normalização simples para faixa 0-1000
        score = raw_score
        if score < 0:
            score = 0.0
        if score > 1000:
            score = 1000.0

        return float(round(score, 0))
