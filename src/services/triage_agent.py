# src/services/triage_agent.py
"""
Agente de Triagem (TriageAgent)

Este módulo concentra a lógica de orquestração do atendimento:
- autenticação do cliente (CPF + data de nascimento);
- oferta de opções de serviço após autenticação;
- roteamento para:
    - crédito (consulta de limite / aumento),
    - entrevista de crédito (recalcular score),
    - câmbio (cotação de moedas).
- tratamento de tentativas, encerramento amigável e retorno ao menu.

A ideia é que o app Streamlit apenas entregue a mensagem do usuário
para este agente e escreva de volta a resposta retornada aqui.
"""

from typing import Dict, Any, Optional, Tuple
import logging
import re

from .ai_dialogue import generate_message
from ..data.client_repository import ClientRepository
from ..utils.validators import extract_cpf, extract_date, normalize_cpf, normalize_date
from ..config import CLIENTS_CSV
from .credit_agent import CreditAgent
from .exchange_agent import ExchangeAgent
from .interview_agent import InterviewAgent

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


# Mapeamento simples de nomes de moedas em português para códigos ISO
_CURRENCY_NAME_MAP = {
    "dólar": "USD", "dolar": "USD",
    "euro": "EUR",
    "real": "BRL", "reais": "BRL",
    "libra": "GBP",
    "iene": "JPY", "yen": "JPY",
    "bitcoin": "BTC", "btc": "BTC",
}


def _is_short_numeric_choice(text: str) -> bool:
    """
    Verifica se o texto é uma escolha numérica curtinha, do tipo "1", "2", "(3)".
    Isso serve para evitar que o usuário pule a autenticação só mandando "1".
    """
    if not text:
        return False
    t = text.strip()
    if re.fullmatch(r"[\(\[]?\d{1,2}[\)\]]?", t):
        return True
    if re.match(r"^\d[\.\-\s]*$", t):
        return True
    return False


class TriageAgent:
    """
    Classe principal do agente de triagem.

    Responsável por:
    - controlar o estado da conversa;
    - lembrar contexto (última ação, histórico simples);
    - chamar os agentes específicos (crédito, entrevista, câmbio);
    - garantir que ninguém fure a autenticação.
    """

    def __init__(self):
        # Infra e dependências
        self.repo = ClientRepository(CLIENTS_CSV)
        self.credit_agent = CreditAgent()
        self.exchange_agent = ExchangeAgent()
        self.interview_agent = InterviewAgent(CLIENTS_CSV)

        # Estado principal da máquina
        self.state: str = "ask_cpf"
        self.attempts: int = 0
        self.max_attempts: int = 3

        # Dados do cliente autenticado
        self.cpf: str = ""
        self.dob: str = ""
        self.authenticated: bool = False
        self.authenticated_name: Optional[str] = None

        # Contexto da conversa / pós-atendimento
        self.last_action: Optional[str] = None  # 'credit' | 'exchange' | 'interview' | None
        self.history: list[str] = []            # histórico simples de ações

        # Flags de fluxo
        self.awaiting_amount_after_show_limit: bool = False

        # Agentes que existem no sistema (pode desabilitar se quiser testar)
        self.available_agents = {
            "credit": True,
            "interview": True,
            "exchange": True,
        }

    # -------------------------------------------------
    # Funções utilitárias de estado e contexto
    # -------------------------------------------------
    def _set_state(self, new_state: str, clear_flags: bool = False):
        """
        Atualiza o estado da conversa com log.
        Se clear_flags=True, limpa alguns marcadores transitórios do fluxo.
        """
        logger.debug("TriageAgent: state %s -> %s", getattr(self, "state", None), new_state)
        self.state = new_state
        if clear_flags:
            self._clear_transient_flags()

    def _clear_transient_flags(self):
        """
        Limpa informações que só fazem sentido dentro de um sub-fluxo
        (por exemplo, logo após mostrar o limite e aguardar valor).
        """
        self.awaiting_amount_after_show_limit = False

    def _push_history(self, action: str):
        """
        Registra a ação no histórico simples. Isso ajuda a interpretar
        pedidos do tipo "de novo" ou "quero continuar naquele negócio de antes".
        """
        if not action:
            return
        self.history.append(action)
        # evita crescer demais (mantém últimas 20 só por segurança)
        if len(self.history) > 20:
            self.history = self.history[-20:]

    # -------------------------------------------------
    # Mensagens auxiliares
    # -------------------------------------------------
    def start(self) -> str:
        """
        Prepara o agente para uma nova triagem e devolve a saudação inicial.
        """
        self.state = "ask_cpf"
        self.attempts = 0
        self.cpf = ""
        self.dob = ""
        self.authenticated = False
        self.authenticated_name = None
        self._clear_transient_flags()
        self.last_action = None
        self.history = []
        return generate_message("greeting")

    def _make_retry_message(self, remaining: int) -> str:
        """
        Mensagem padrão quando a autenticação falha, mostrando tentativas restantes.
        """
        return (
            f"Não autenticado — restam {remaining} tentativa"
            f"{'s' if remaining != 1 else ''}. Verifique seus dados (CPF e data de nascimento) "
            "e tente novamente."
        )

    def _build_post_auth_message(self, name: str) -> str:
        """
        Monta o menu principal após autenticação.
        """
        parts = [
            f"Você foi autenticado, {name}. Em que posso ajudar?",
            "Opções:",
            "(1) Crédito — consultar limite ou solicitar aumento",
            "(2) Entrevista de crédito para tentar melhorar o score",
            "(3) Consultar cotação de moedas",
            "Por favor escolha 1, 2 ou 3, ou descreva o que deseja."
        ]
        return " ".join(parts)

    # -------------------------------------------------
    # Funções auxiliares de parsing (valores, ações, câmbio)
    # -------------------------------------------------
    def _extract_amount(self, text: str) -> Optional[float]:
        """
        Faz parsing de valores numéricos em formatos diversos:
        - "8000"
        - "8.000"
        - "8,000.50"
        - "8 mil"
        - "8k"
        etc.
        """
        if not text:
            return None
        t = text.lower()
        t = re.sub(r"[r$\s]", "", t)
        t = t.replace(".", "").replace(",", ".")
        m = re.search(r"(\d+(?:\.\d+)?)\s*(k|mil|m)?", t)
        if m:
            val = float(m.group(1))
            suf = m.group(2)
            if suf:
                suf = suf.lower()
                if suf in ("k", "mil"):
                    val *= 1000.0
                elif suf == "m":
                    val *= 1_000_000.0
            return val
        m2 = re.search(r"(\d+(?:\.\d{1,2})?)", text.replace(",", "."))
        if m2:
            try:
                return float(m2.group(1))
            except Exception:
                return None
        return None

    def _is_asking_current_limit(self, text: str) -> bool:
        """
        Detecta se o usuário está perguntando qual é o limite atual.
        """
        if not text:
            return False
        t = text.lower()
        keywords = [
            "qual é o meu limite", "qual meu limite", "limite atual",
            "esqueci meu limite", "meu limite", "meu crédito", "meu credito"
        ]
        return any(k in t for k in keywords)

    def _parse_exchange_text(self, text: str) -> Tuple[str, str]:
        """
        Converte o texto do usuário em par (base, target) de moedas.
        Ex.: "usd para brl", "dólar para real", "eur".
        """
        if not text:
            return ("USD", "BRL")
        codes = re.findall(r"\b([A-Za-z]{3})\b", text)
        if len(codes) >= 2:
            return (codes[0].upper(), codes[1].upper())
        if len(codes) == 1:
            return (codes[0].upper(), "BRL")

        t = text.strip().lower()
        m = re.search(r"([a-zà-ú]+)\s+(?:para|to|->)\s+([a-zà-ú]+)", t)
        if m:
            base_name = m.group(1)
            target_name = m.group(2)
            base_code = _CURRENCY_NAME_MAP.get(base_name, base_name[:3].upper())
            target_code = _CURRENCY_NAME_MAP.get(target_name, target_name[:3].upper())
            return (base_code, target_code)

        for name, code in _CURRENCY_NAME_MAP.items():
            if name in t:
                return (code, "BRL")

        return ("USD", "BRL")

    def _interpret_action_choice(self, text: str) -> str:
        """
        Interpreta o menu principal (1/2/3 ou texto).
        Retorno normalizado: 'credito' | 'interview' | 'cambio' | ''.
        """
        if not text:
            return ""
        t = text.strip().lower()

        if t in ("1", "1)", "(1)"):
            return "credito"
        if t in ("2", "2)", "(2)"):
            return "interview"
        if t in ("3", "3)", "(3)"):
            return "cambio"

        if any(k in t for k in ["credito", "crédito", "limite", "aumento"]):
            return "credito"
        if any(k in t for k in ["entrevista", "score", "pontuação", "pontuacao"]):
            return "interview"
        if any(k in t for k in ["câmbio", "cambio", "cotação", "cotacao", "moeda", "dólar", "euro", "usd", "eur", "brl"]):
            return "cambio"

        return ""

    def _interpret_credit_action(self, text: str) -> str:
        """
        Interpreta as opções de crédito:
        - 'consultar' limite
        - 'solicitar' aumento
        """
        if not text:
            return ""
        t = text.strip().lower()

        if t in ("1", "1)", "(1)"):
            return "consultar"
        if t in ("2", "2)", "(2)"):
            return "solicitar"

        if any(k in t for k in ["consultar", "consulta", "ver limite", "limite atual", "meu limite"]):
            return "consultar"
        if any(k in t for k in ["solicitar", "solicitação", "aumentar", "aumento", "novo limite", "pedir aumento"]):
            return "solicitar"

        return ""

    # -------------------------------------------------
    # Handler principal — ponto de entrada
    # -------------------------------------------------
    def handle_user(self, user_text: str) -> Dict[str, Any]:
        """
        Recebe a mensagem do usuário e devolve:
          { "assistant": str, "done": bool }

        Esse método é o único que o front (Streamlit) precisa chamar.
        """
        text = (user_text or "").strip()
        l = text.lower()

        # Comando de saída a qualquer momento
        if any(k in l for k in ("encerrar", "fim", "sair", "cancelar", "tchau")):
            self._set_state("final", clear_flags=True)
            return {
                "assistant": "Conversa encerrada. Obrigado por usar o Banco Ágil.",
                "done": True,
            }

        # Se já autenticado mas por algum motivo o estado ficou para trás, força para pós-auth
        if self.authenticated and self.state in ("ask_cpf", "ask_dob"):
            self._set_state("post_auth", clear_flags=False)

        # Antes de autenticar, não deixa usar o menu por número
        if not self.authenticated and _is_short_numeric_choice(text):
            return {
                "assistant": (
                    "Para prosseguir com as opções, preciso primeiro autenticar você. "
                    "Por favor, informe seu CPF (somente números, sem pontos ou traços)."
                ),
                "done": False,
            }

        # ----------------- ESTADO: ask_cpf -----------------
        if self.state == "ask_cpf":
            cpf_found = extract_cpf(text)
            date_in_msg = extract_date(text)

            if not cpf_found:
                return {"assistant": generate_message("ask_cpf", user_message=text), "done": False}

            self.cpf = normalize_cpf(cpf_found)

            if date_in_msg:
                self.dob = normalize_date(date_in_msg)
                candidate = self.repo.find_by_cpf_and_dob(self.cpf, self.dob)
                if candidate:
                    self.authenticated = True
                    self.authenticated_name = candidate.get("nome") or "cliente"
                    self._set_state("post_auth", clear_flags=True)
                    return {
                        "assistant": self._build_post_auth_message(self.authenticated_name),
                        "done": False,
                    }

                # falha com CPF + data na mesma mensagem
                self.attempts += 1
                remaining = max(self.max_attempts - self.attempts, 0)
                if self.attempts >= self.max_attempts:
                    self._set_state("final", clear_flags=True)
                    return {
                        "assistant": (
                            "Não foi possível autenticar após 3 tentativas. "
                            "Por favor, tente novamente mais tarde ou contate o suporte."
                        ),
                        "done": True,
                    }
                self.cpf = ""
                self.dob = ""
                self._set_state("ask_cpf", clear_flags=True)
                return {"assistant": self._make_retry_message(remaining), "done": False}

            # segue para data de nascimento
            self._set_state("ask_dob", clear_flags=False)
            return {"assistant": generate_message("ask_dob"), "done": False}

        # ----------------- ESTADO: ask_dob -----------------
        if self.state == "ask_dob":
            dob_found = extract_date(text)
            if not dob_found:
                return {
                    "assistant": (
                        "Esse formato está incorreto. Por favor, informe sua data de nascimento "
                        "no formato DD/MM/AAAA. Exemplo: 07/07/1985."
                    ),
                    "done": False,
                }

            self.dob = normalize_date(dob_found)
            candidate = self.repo.find_by_cpf_and_dob(self.cpf, self.dob)
            if candidate:
                self.authenticated = True
                self.authenticated_name = candidate.get("nome") or "cliente"
                self._set_state("post_auth", clear_flags=True)
                return {
                    "assistant": self._build_post_auth_message(self.authenticated_name),
                    "done": False,
                }

            # falha na autenticação
            self.attempts += 1
            remaining = max(self.max_attempts - self.attempts, 0)
            if self.attempts >= self.max_attempts:
                self._set_state("final", clear_flags=True)
                return {
                    "assistant": (
                        "Não foi possível autenticar após 3 tentativas. "
                        "Por favor, tente novamente mais tarde ou contate o suporte."
                    ),
                    "done": True,
                }
            self.cpf = ""
            self.dob = ""
            self._set_state("ask_cpf", clear_flags=True)
            return {"assistant": self._make_retry_message(remaining), "done": False}

        # ----------------- ESTADO: post_auth -----------------
        if self.state == "post_auth":
            choice = self._interpret_action_choice(text)

            # caso o usuário fale algo tipo "de novo" sem dizer qual
            if not choice:
                if any(k in l for k in ("de novo", "novamente", "repetir", "igual da outra vez")) and self.history:
                    last = self.history[-1]
                    if last == "credit":
                        choice = "credito"
                    elif last == "exchange":
                        choice = "cambio"
                    elif last == "interview":
                        choice = "interview"

            if choice == "credito":
                if self.available_agents.get("credit"):
                    self.last_action = "credit"
                    self._push_history("credit")
                    self._set_state("ask_credit_action", clear_flags=True)
                    return {"assistant": generate_message("ask_credit_action"), "done": False}
                return {
                    "assistant": "Serviço de crédito indisponível no momento. " + generate_message("ask_more"),
                    "done": False,
                }

            if choice == "interview":
                if self.available_agents.get("interview"):
                    if not self.authenticated or not self.cpf:
                        return {
                            "assistant": "Preciso autenticar sua conta primeiro. Informe seu CPF (somente números).",
                            "done": False,
                        }
                    self.last_action = "interview"
                    self._push_history("interview")
                    start_msg = self.interview_agent.start(self.cpf)
                    self._set_state("interview_running", clear_flags=True)
                    return {"assistant": start_msg, "done": False}

                self._set_state("confirm_redirect_credit", clear_flags=False)
                return {
                    "assistant": (
                        "No momento a entrevista para ajuste de score não está disponível. "
                        "Posso abrir as opções de Crédito (consultar limite / solicitar aumento) para você? (responda sim ou não)"
                    ),
                    "done": False,
                }

            if choice == "cambio":
                if self.available_agents.get("exchange"):
                    self.last_action = "exchange"
                    self._push_history("exchange")
                    self._set_state("exchange_ask_currency", clear_flags=True)
                    return {"assistant": generate_message("ask_exchange"), "done": False}
                return {
                    "assistant": "Serviço de câmbio indisponível no momento. " + generate_message("ask_more"),
                    "done": False,
                }

            # se não entendeu, reapresenta o menu
            return {
                "assistant": self._build_post_auth_message(self.authenticated_name or "cliente"),
                "done": False,
            }

        # ----------------- ESTADO: confirm_redirect_credit -----------------
        if self.state == "confirm_redirect_credit":
            if any(k in l for k in ("sim", "s", "quero", "ok", "yes")):
                self.last_action = "credit"
                self._push_history("credit")
                self._set_state("ask_credit_action", clear_flags=True)
                return {"assistant": generate_message("ask_credit_action"), "done": False}
            if any(k in l for k in ("não", "nao", "n")):
                self._set_state("ask_more", clear_flags=False)
                return {"assistant": generate_message("ask_more"), "done": False}
            return {"assistant": "Por favor responda 'sim' ou 'não'.", "done": False}

        # ----------------- ESTADO: ask_credit_action -----------------
        if self.state == "ask_credit_action":
            sub = self._interpret_credit_action(text)
            if not sub:
                return {
                    "assistant": generate_message(
                        "ask_credit_action",
                        user_message="Por favor escolha 1 ou 2 (consultar limite / solicitar aumento)."
                    ),
                    "done": False,
                }

            # consultar limite
            if sub == "consultar":
                info = self.credit_agent.consulta_limite(self.cpf)
                if not info.get("ok"):
                    self._set_state("ask_more", clear_flags=False)
                    return {
                        "assistant": "Não foi possível encontrar seus dados. " + generate_message("ask_more"),
                        "done": False,
                    }
                limite = info.get("limite_atual", 0.0)
                name = self.authenticated_name or "cliente"
                self.last_action = "credit"
                self._push_history("credit")
                self._set_state("ask_more", clear_flags=True)
                return {
                    "assistant": f"Seu limite atual é R$ {limite:.2f}. Obrigado, {name}. " + generate_message("ask_more"),
                    "done": False,
                }

            # solicitar aumento
            self._set_state("ask_credit_amount", clear_flags=True)
            return {"assistant": generate_message("ask_credit_amount"), "done": False}

        # ----------------- ESTADO: ask_credit_amount -----------------
        if self.state == "ask_credit_amount":
            amount = self._extract_amount(text)
            asks_limit = self._is_asking_current_limit(text)

            # usuário pediu para ver o limite atual primeiro
            if asks_limit and amount is None:
                info = self.credit_agent.consulta_limite(self.cpf)
                if not info.get("ok"):
                    self._set_state("ask_more", clear_flags=False)
                    return {
                        "assistant": "Não foi possível localizar seus dados. " + generate_message("ask_more"),
                        "done": False,
                    }
                limite = info.get("limite_atual", 0.0)
                self.awaiting_amount_after_show_limit = True
                return {
                    "assistant": (
                        f"Seu limite atual é R$ {limite:.2f}. Deseja solicitar aumento de limite? "
                        "Se sim, informe o novo valor (apenas números) agora; se não, responda 'não'."
                    ),
                    "done": False,
                }

            # fluxo em que já mostramos o limite e agora esperamos valor ou 'não'
            if self.awaiting_amount_after_show_limit:
                if any(k in l for k in ("não", "nao", "n")):
                    self.awaiting_amount_after_show_limit = False
                    self._set_state("ask_more", clear_flags=False)
                    return {"assistant": generate_message("ask_more"), "done": False}

                if amount is not None:
                    res = self.credit_agent.solicitar_aumento(self.cpf, float(amount))
                    self.awaiting_amount_after_show_limit = False

                    if not res.get("ok"):
                        self._set_state("ask_more", clear_flags=False)
                        return {
                            "assistant": "Não foi possível processar sua solicitação. " + generate_message("ask_more"),
                            "done": False,
                        }

                    status = res.get("status_pedido")
                    reason = res.get("reason", "")
                    name = self.authenticated_name or "cliente"
                    self.last_action = "credit"
                    self._push_history("credit")

                    if status == "aprovado":
                        self._set_state("ask_more", clear_flags=True)
                        return {
                            "assistant": f"Solicitação aprovada. {reason} Obrigado, {name}. " + generate_message("ask_more"),
                            "done": False,
                        }

                    self._set_state("offer_interview", clear_flags=False)
                    return {
                        "assistant": (
                            f"Solicitação rejeitada. {reason} "
                            "Deseja ser encaminhado para a entrevista de crédito para tentar reajustar seu score? (responda sim ou não)"
                        ),
                        "done": False,
                    }

                return {
                    "assistant": "Se deseja solicitar aumento, informe o novo valor (apenas números) ou responda 'não' para encerrar.",
                    "done": False,
                }

            # usuário falou algo tipo "qual meu limite e quero 8000"
            if asks_limit and amount is not None:
                res = self.credit_agent.solicitar_aumento(self.cpf, float(amount))
                if not res.get("ok"):
                    self._set_state("ask_more", clear_flags=False)
                    return {
                        "assistant": "Não foi possível processar sua solicitação. " + generate_message("ask_more"),
                        "done": False,
                    }
                status = res.get("status_pedido")
                reason = res.get("reason", "")
                name = self.authenticated_name or "cliente"
                self.last_action = "credit"
                self._push_history("credit")
                if status == "aprovado":
                    self._set_state("ask_more", clear_flags=True)
                    return {
                        "assistant": f"Solicitação aprovada. {reason} Obrigado, {name}. " + generate_message("ask_more"),
                        "done": False,
                    }
                self._set_state("offer_interview", clear_flags=False)
                return {
                    "assistant": (
                        f"Solicitação rejeitada. {reason} "
                        "Deseja ser encaminhado para a entrevista de crédito para tentar reajustar seu score? (responda sim ou não)"
                    ),
                    "done": False,
                }

            # não entendeu o valor
            if amount is None:
                return {
                    "assistant": generate_message(
                        "ask_credit_amount",
                        user_message="Não entendi o valor. Informe apenas números, ex.: 5000 ou 1500.50."
                    ),
                    "done": False,
                }

            # fluxo normal: valor informado direto
            res = self.credit_agent.solicitar_aumento(self.cpf, float(amount))
            if not res.get("ok"):
                self._set_state("ask_more", clear_flags=False)
                return {
                    "assistant": "Não foi possível processar sua solicitação. " + generate_message("ask_more"),
                    "done": False,
                }

            status = res.get("status_pedido")
            reason = res.get("reason", "")
            name = self.authenticated_name or "cliente"
            self.last_action = "credit"
            self._push_history("credit")

            if status == "aprovado":
                self._set_state("ask_more", clear_flags=True)
                return {
                    "assistant": f"Solicitação aprovada. {reason} Obrigado, {name}. " + generate_message("ask_more"),
                    "done": False,
                }

            self._set_state("offer_interview", clear_flags=False)
            return {
                "assistant": (
                    f"Solicitação rejeitada. {reason} "
                    "Deseja ser encaminhado para a entrevista de crédito para tentar reajustar seu score? (responda sim ou não)"
                ),
                "done": False,
            }

        # ----------------- ESTADO: exchange_ask_currency -----------------
        if self.state == "exchange_ask_currency":
            base, target = self._parse_exchange_text(text)
            self._clear_transient_flags()
            res = self.exchange_agent.get_rate(base=base, target=target)

            if not res.get("ok"):
                self.last_action = "exchange"
                self._push_history("exchange")
                self._set_state("ask_more", clear_flags=False)
                return {
                    "assistant": f"{res.get('msg')} {generate_message('ask_more')}",
                    "done": False,
                }

            rate = res.get("rate")
            rate_short = f"{rate:.2f}"
            self.last_action = "exchange"
            self._push_history("exchange")
            self._set_state("ask_more", clear_flags=False)
            return {
                "assistant": (
                    f"Cotação atual: 1 {res.get('base')} = {rate_short} {res.get('target')}. "
                    + generate_message("ask_more")
                ),
                "done": False,
            }

        # ----------------- ESTADO: interview_running -----------------
        if self.state == "interview_running":
            out = self.interview_agent.handle(text)
            assistant_text = out.get("assistant", "Desculpe, ocorreu um erro na entrevista.")
            done_flag = out.get("done", False)
            redirect = out.get("redirect")

            if done_flag:
                if redirect == "credit":
                    self.last_action = "credit"
                    self._push_history("credit")
                    self._set_state("ask_credit_action", clear_flags=True)
                    follow = "Agora vou abrir as opções de crédito para nova análise. "
                    return {
                        "assistant": f"{assistant_text} {follow}{generate_message('ask_credit_action')}",
                        "done": False,
                    }
                self._set_state("post_auth", clear_flags=False)
                return {
                    "assistant": f"{assistant_text} {self._build_post_auth_message(self.authenticated_name or 'cliente')}",
                    "done": False,
                }

            return {"assistant": assistant_text, "done": False}

        # ----------------- ESTADO: offer_interview -----------------
        if self.state == "offer_interview":
            if any(k in l for k in ("sim", "s", "quero", "ok", "yes")):
                if self.available_agents.get("interview"):
                    if not self.authenticated or not self.cpf:
                        return {
                            "assistant": "Preciso autenticar sua conta primeiro. Informe seu CPF (somente números).",
                            "done": False,
                        }
                    self.last_action = "interview"
                    self._push_history("interview")
                    start_msg = self.interview_agent.start(self.cpf)
                    self._set_state("interview_running", clear_flags=True)
                    return {"assistant": start_msg, "done": False}
                self._set_state("ask_more", clear_flags=False)
                return {
                    "assistant": "No momento a entrevista de crédito não está implementada. " + generate_message("ask_more"),
                    "done": False,
                }

            if any(k in l for k in ("não", "nao", "n")):
                self._set_state("ask_more", clear_flags=False)
                return {"assistant": generate_message("ask_more"), "done": False}

            return {
                "assistant": "Não entendi. Deseja seguir para a entrevista de crédito? Responda 'sim' ou 'não'.",
                "done": False,
            }

        # ----------------- ESTADO: ask_more (pós-ação genérico) -----------------
        if self.state == "ask_more":
            txt = l

            # usuário não quer mais nada
            if any(k in txt for k in ("não", "nao", "n", "não, obrigado", "nao, obrigado")):
                self._set_state("final", clear_flags=True)
                return {
                    "assistant": "Obrigado por usar o Banco Ágil. Tenha um bom dia!",
                    "done": True,
                }

            # usuário quer ver o menu
            if "menu" in txt or "opções" in txt or "opcoes" in txt:
                self._set_state("post_auth", clear_flags=False)
                return {
                    "assistant": self._build_post_auth_message(self.authenticated_name or "cliente"),
                    "done": False,
                }

            # respondeu "sim" → cuidamos com base na última ação
            if any(k in txt for k in ("sim", "s", "yes", "ok", "claro")):
                # se veio de câmbio, pergunta se quer outra cotação ou menu
                if self.last_action == "exchange":
                    self._set_state("exchange_more_menu", clear_flags=False)
                    return {
                        "assistant": (
                            "Quer consultar outra cotação de moeda ou voltar ao menu principal?\n"
                            "- Digite 'cotação' ou 'moeda' para ver outra moeda.\n"
                            "- Digite 'menu' para voltar às opções principais."
                        ),
                        "done": False,
                    }
                # se veio de crédito, oferece as opções de crédito ou menu
                if self.last_action == "credit":
                    self._set_state("credit_more_menu", clear_flags=False)
                    return {
                        "assistant": (
                            "Deseja consultar crédito novamente? Escolha uma opção:\n"
                            "- 'consultar' para ver o limite atual\n"
                            "- 'solicitar' para pedir um novo aumento de limite\n"
                            "Ou escreva 'menu' para ver outras opções."
                        ),
                        "done": False,
                    }
                # sem última ação definida → volta pro menu
                self._set_state("post_auth", clear_flags=False)
                return {
                    "assistant": self._build_post_auth_message(self.authenticated_name or "cliente"),
                    "done": False,
                }

            # usuário pede explicitamente algo relacionado (cotação/crédito) sem dizer sim
            if re.search(r"\b(cotar|cotação|cotacao|moeda)\b", txt):
                self.last_action = "exchange"
                self._push_history("exchange")
                self._set_state("exchange_ask_currency", clear_flags=True)
                return {"assistant": generate_message("ask_exchange"), "done": False}

            if re.search(r"\b(limite|crédito|credito|aumento)\b", txt):
                self.last_action = "credit"
                self._push_history("credit")
                self._set_state("ask_credit_action", clear_flags=True)
                return {"assistant": generate_message("ask_credit_action"), "done": False}

            # fallback: orienta usuário a responder algo compreensível
            return {
                "assistant": (
                    "Por favor responda 'sim' ou 'não'. Se quiser ver o menu, escreva 'menu'. "
                    "Se quiser continuar em crédito, mencione 'limite' ou 'aumento'. "
                    "Para câmbio, escreva 'cotação' ou 'moeda'."
                ),
                "done": False,
            }

        # ----------------- ESTADO: credit_more_menu -----------------
        if self.state == "credit_more_menu":
            txt = l

            # usuário pediu o menu de novo
            if "menu" in txt:
                self._set_state("post_auth", clear_flags=False)
                return {
                    "assistant": self._build_post_auth_message(self.authenticated_name or "cliente"),
                    "done": False,
                }

            sub = self._interpret_credit_action(text)
            if sub == "consultar":
                info = self.credit_agent.consulta_limite(self.cpf)
                if not info.get("ok"):
                    self._set_state("ask_more", clear_flags=False)
                    return {
                        "assistant": "Não foi possível encontrar seus dados. " + generate_message("ask_more"),
                        "done": False,
                    }
                limite = info.get("limite_atual", 0.0)
                name = self.authenticated_name or "cliente"
                self.last_action = "credit"
                self._push_history("credit")
                self._set_state("ask_more", clear_flags=True)
                return {
                    "assistant": f"Seu limite atual é R$ {limite:.2f}. Obrigado, {name}. " + generate_message("ask_more"),
                    "done": False,
                }

            if sub == "solicitar":
                self.last_action = "credit"
                self._push_history("credit")
                self._set_state("ask_credit_amount", clear_flags=True)
                return {"assistant": generate_message("ask_credit_amount"), "done": False}

            return {
                "assistant": (
                    "Não entendi. Digite 'consultar', 'solicitar' ou 'menu' para voltar às opções principais."
                ),
                "done": False,
            }

        # ----------------- ESTADO: exchange_more_menu -----------------
        if self.state == "exchange_more_menu":
            txt = l

            if "menu" in txt:
                self._set_state("post_auth", clear_flags=False)
                return {
                    "assistant": self._build_post_auth_message(self.authenticated_name or "cliente"),
                    "done": False,
                }

            if any(k in txt for k in ("cotação", "cotacao", "moeda", "outra", "mais", "sim", "s")):
                self.last_action = "exchange"
                self._push_history("exchange")
                self._set_state("exchange_ask_currency", clear_flags=True)
                return {"assistant": generate_message("ask_exchange"), "done": False}

            if any(k in txt for k in ("não", "nao", "n")):
                self._set_state("final", clear_flags=True)
                return {
                    "assistant": "Obrigado por usar o Banco Ágil. Tenha um bom dia!",
                    "done": True,
                }

            return {
                "assistant": (
                    "Quer ver outra cotação ou voltar ao menu? Escreva 'moeda' para nova cotação ou 'menu' para as opções."
                ),
                "done": False,
            }

        # ----------------- fallback geral -----------------
        self._set_state("final", clear_flags=True)
        return {
            "assistant": "Atendimento finalizado.",
            "done": True,
        }
