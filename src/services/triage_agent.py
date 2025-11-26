# src/services/triage_agent.py
"""
Agente de Triagem (TriageAgent) - versão ajustada para frases de crédito corretas.

Comentários por bloco:
- Inicialização: cria repos, agentes auxiliares e estado inicial.
- Helpers: funções utilitárias para parsing (valores, moedas, intenções).
- handle_user: núcleo que processa a mensagem do usuário e retorna resposta.
- Mensagens auxiliares: construção de menus e mensagens de retry.
Observação: comentários escritos em tom humano e técnico, sem indicação de autoria por IA.
"""

from typing import Dict, Any, Optional, Tuple
import logging
import re
import hashlib

from .ai_dialogue import generate_message
from ..data.client_repository import ClientRepository
from ..utils.validators import extract_cpf, extract_date, normalize_cpf, normalize_date
from ..config import CLIENTS_CSV
from .credit_agent import CreditAgent
from .exchange_agent import ExchangeAgent
from .interview_agent import InterviewAgent

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_CURRENCY_NAME_MAP = {
    "dólar": "USD", "dolar": "USD", "euro": "EUR", "reais": "BRL", "real": "BRL",
    "libra": "GBP", "bitcoin": "BTC", "btc": "BTC", "yen": "JPY", "iene": "JPY"
}


def _is_short_numeric_choice(text: str) -> bool:
    """
    Detecta entradas curtas que aparentam ser escolhas de menu, para evitar interpretar
    '1'/'2' antes do usuário estar autenticado.
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
    Classe que orquestra o atendimento:
    - autenticação (CPF + DOB)
    - roteamento para crédito, entrevista, câmbio
    - manutenção de estado da sessão e flags de contexto
    """

    def __init__(self):
        # Repositório de clientes e agentes auxiliares
        self.repo = ClientRepository(CLIENTS_CSV)
        self.credit_agent = CreditAgent()
        self.exchange_agent = ExchangeAgent()
        self.interview_agent = InterviewAgent(CLIENTS_CSV)

        # Estado conversacional
        self.state: str = "ask_cpf"
        self.attempts: int = 0
        self.max_attempts: int = 3

        # Dados de autenticação
        self.cpf: str = ""
        self.dob: str = ""
        self.authenticated: bool = False
        self.authenticated_name: Optional[str] = None

        # Flags de fluxo / último contexto usado
        self.awaiting_amount_after_show_limit: bool = False
        self.last_action: Optional[str] = None  # 'credit'|'exchange'|None

        # Disponibilidade de agentes (toggle para deploy/testes)
        self.available_agents = {"credit": True, "interview": True, "exchange": True}

    # ---------------------------
    # Helpers / utilitários
    # ---------------------------
    def start(self) -> str:
        """
        Prepara o agente para uma nova sessão e retorna mensagem inicial.
        """
        self.state = "ask_cpf"
        self._clear_transient_flags()
        return generate_message("greeting")

    def _clear_transient_flags(self):
        """Limpa sinais transitórios entre fluxos (não remove autenticação)."""
        self.awaiting_amount_after_show_limit = False

    def _extract_amount(self, text: str) -> Optional[float]:
        """
        Parser tolerante para valores: '8 mil', '8k', '8.000', '1.500,50', '1500.50'.
        Retorna float ou None.
        """
        if not text:
            return None
        t = text.lower()
        t = re.sub(r"[R$€£\s]", "", t)
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
                    val *= 1000000.0
            return val
        m2 = re.search(r"(\d+(?:\.\d{1,2})?)", text.replace(",", "."))
        if m2:
            try:
                return float(m2.group(1))
            except Exception:
                return None
        return None

    def _is_asking_current_limit(self, text: str) -> bool:
        """Detecta se o usuário está pedindo para ver o limite atual."""
        if not text:
            return False
        t = text.lower()
        keywords = [
            "qual é o meu limite", "qual meu limite", "limite atual", "esqueci",
            "meu limite", "meu saldo", "qual o meu limite", "qual é meu limite"
        ]
        return any(k in t for k in keywords)

    def _parse_exchange_text(self, text: str) -> Tuple[str, str]:
        """
        Normaliza texto de câmbio para pares de moedas (base, target).
        Suporta 'USD para BRL', 'EUR', 'dólar para real', etc.
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
        Interpreta o menu principal (1/2/3 ou palavras-chave).
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
        if any(k in t for k in ["credito", "limite", "aumento"]):
            return "credito"
        if any(k in t for k in ["entrevista", "entrevistar", "score", "pontuação", "pontuacao"]):
            return "interview"
        if any(k in t for k in ["câmbio", "cambio", "cotação", "cotacao", "dólar", "euro", "usd", "eur", "brl", "moeda"]):
            return "cambio"
        return ""

    def _interpret_credit_action(self, text: str) -> str:
        """
        Interpreta as opções no fluxo de crédito: consultar ou solicitar aumento.
        """
        if not text:
            return ""
        t = text.strip().lower()
        if t in ("1", "1)", "(1)"):
            return "consultar"
        if t in ("2", "2)", "(2)"):
            return "solicitar"
        if any(k in t for k in ["consultar", "consulta", "ver limite", "limite atual", "meu limite", "meu saldo"]):
            return "consultar"
        if any(k in t for k in ["solicitar", "solicitação", "aumentar", "aumento", "novo limite", "pedir aumento"]):
            return "solicitar"
        return ""

    # ---------------------------
    # Handler principal (entrada)
    # ---------------------------
    def handle_user(self, user_text: str) -> Dict[str, Any]:
        """
        Processa a mensagem do usuário e retorna {'assistant': str, 'done': bool}.
        'done' indica encerramento do atendimento.
        """
        text = (user_text or "").strip()
        l = text.lower()

        # Comandos de saída
        if any(k in l for k in ("encerrar", "fim", "sair", "cancelar", "tchau")):
            return {"assistant": "Conversa encerrada. Obrigado por usar o Banco Ágil.", "done": True}

        # Se já autenticado mas o estado ficou travado em ask_cpf/ask_dob, restaurar para post_auth
        if self.authenticated and self.state in ("ask_cpf", "ask_dob"):
            self.state = "post_auth"

        # Bloqueia interpretação de '1'/'2' antes da autenticação
        if not self.authenticated and _is_short_numeric_choice(text):
            return {"assistant": "Para prosseguir com as opções preciso autenticar você primeiro. Por favor informe seu CPF (somente números, sem pontos ou traços).", "done": False}

        # ---------------- ASK_CPF ----------------
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
                    self.state = "post_auth"
                    self._clear_transient_flags()
                    return {"assistant": self._build_post_auth_message(self.authenticated_name), "done": False}
                else:
                    self.attempts += 1
                    remaining = max(self.max_attempts - self.attempts, 0)
                    if self.attempts >= self.max_attempts:
                        return {"assistant": "Não foi possível autenticar após 3 tentativas. Por favor tente novamente mais tarde ou contate o suporte.", "done": True}
                    self.cpf = ""
                    self.dob = ""
                    self.state = "ask_cpf"
                    return {"assistant": self._make_retry_message(remaining), "done": False}

            self.state = "ask_dob"
            return {"assistant": generate_message("ask_dob"), "done": False}

        # ---------------- ASK_DOB ----------------
        if self.state == "ask_dob":
            dob_found = extract_date(text)
            if not dob_found:
                return {"assistant": "Formato incorreto — por favor informe sua data de nascimento no formato DD/MM/AAAA. Ex.: 07/07/1985.", "done": False}
            self.dob = normalize_date(dob_found)
            candidate = self.repo.find_by_cpf_and_dob(self.cpf, self.dob)
            if candidate:
                self.authenticated = True
                self.authenticated_name = candidate.get("nome") or "cliente"
                self.state = "post_auth"
                self._clear_transient_flags()
                return {"assistant": self._build_post_auth_message(self.authenticated_name), "done": False}
            else:
                self.attempts += 1
                remaining = max(self.max_attempts - self.attempts, 0)
                if self.attempts >= self.max_attempts:
                    return {"assistant": "Não foi possível autenticar após 3 tentativas. Por favor tente novamente mais tarde ou contate o suporte.", "done": True}
                self.cpf = ""
                self.dob = ""
                self.state = "ask_cpf"
                return {"assistant": self._make_retry_message(remaining), "done": False}

        # ---------------- POST_AUTH ----------------
        if self.state == "post_auth":
            choice = self._interpret_action_choice(text)
            if choice == "credito":
                if self.available_agents.get("credit"):
                    self._clear_transient_flags()
                    self.state = "ask_credit_action"
                    return {"assistant": generate_message("ask_credit_action"), "done": False}
                return {"assistant": "Serviço de crédito indisponível no momento. " + generate_message("ask_more"), "done": False}

            if choice == "interview":
                if self.available_agents.get("interview"):
                    if not self.authenticated or not self.cpf:
                        return {"assistant": "Preciso autenticar sua conta primeiro. Por favor informe seu CPF (somente números).", "done": False}
                    start_msg = self.interview_agent.start(self.cpf)
                    self.state = "interview_running"
                    return {"assistant": start_msg, "done": False}
                self.state = "confirm_redirect_credit"
                return {"assistant": ("No momento a entrevista para ajuste de score não está disponível. "
                                      "Posso abrir as opções de Crédito (consultar limite / solicitar aumento) para você? (responda sim ou não)"), "done": False}

            if choice == "cambio":
                if self.available_agents.get("exchange"):
                    self._clear_transient_flags()
                    self.state = "exchange_ask_currency"
                    return {"assistant": generate_message("ask_exchange"), "done": False}
                return {"assistant": "Serviço de câmbio indisponível no momento. " + generate_message("ask_more"), "done": False}

            # Reenvia o menu principal se não entendeu intenção
            return {"assistant": self._build_post_auth_message(self.authenticated_name or "cliente"), "done": False}

        # ---------------- CONFIRM REDIRECT TO CREDIT ----------------
        if self.state == "confirm_redirect_credit":
            txt = l
            if any(k in txt for k in ("sim", "s", "quero", "ok", "yes")):
                self._clear_transient_flags()
                self.state = "ask_credit_action"
                return {"assistant": generate_message("ask_credit_action"), "done": False}
            if any(k in txt for k in ("não", "nao", "n")):
                self.state = "ask_more"
                return {"assistant": generate_message("ask_more"), "done": False}
            return {"assistant": "Por favor responda sim ou não.", "done": False}

        # ---------------- ASK_CREDIT_ACTION ----------------
        if self.state == "ask_credit_action":
            sub = self._interpret_credit_action(text)
            if not sub:
                # uso do fallback ajustado em ai_dialogue.ask_credit_action
                return {"assistant": generate_message("ask_credit_action", user_message="Por favor escolha 1 ou 2 (consultar/solicitar)."), "done": False}
            if sub == "consultar":
                info = self.credit_agent.consulta_limite(self.cpf)
                if not info.get("ok"):
                    return {"assistant": "Não foi possível encontrar seus dados. " + generate_message("ask_more"), "done": False}
                limite = info.get("limite_atual", 0.0)
                name = self.authenticated_name or "cliente"
                self.last_action = "credit"
                self.state = "ask_more"
                return {"assistant": f"Seu limite atual é R$ {limite:.2f}. Obrigado, {name}. " + generate_message("ask_more"), "done": False}
            # solicitar aumento
            self._clear_transient_flags()
            self.state = "ask_credit_amount"
            return {"assistant": generate_message("ask_credit_amount"), "done": False}

        # ---------------- ASK_CREDIT_AMOUNT ----------------
        if self.state == "ask_credit_amount":
            amount = self._extract_amount(text)
            asks_limit = self._is_asking_current_limit(text)

            if asks_limit and amount is None:
                info = self.credit_agent.consulta_limite(self.cpf)
                if not info.get("ok"):
                    return {"assistant": "Não foi possível localizar seus dados. " + generate_message("ask_more"), "done": False}
                limite = info.get("limite_atual", 0.0)
                self.awaiting_amount_after_show_limit = True
                return {"assistant": (f"Seu limite atual é R$ {limite:.2f}. Deseja solicitar aumento de limite? "
                                      "Se sim, informe o novo valor (apenas números) agora; se não, responda 'não'."), "done": False}

            if asks_limit and amount is not None:
                res = self.credit_agent.solicitar_aumento(self.cpf, float(amount))
                if not res.get("ok"):
                    return {"assistant": "Não foi possível processar sua solicitação. " + generate_message("ask_more"), "done": False}
                status = res.get("status_pedido")
                reason = res.get("reason", "")
                name = self.authenticated_name or "cliente"
                self.last_action = "credit"
                if status == "aprovado":
                    self.state = "ask_more"
                    return {"assistant": f"Solicitação aprovada. {reason} Obrigado, {name}. " + generate_message("ask_more"), "done": False}
                self.state = "offer_interview"
                return {"assistant": f"Solicitação rejeitada. {reason} Deseja ser encaminhado para a entrevista de crédito para tentar reajustar seu score? (responda sim ou não)", "done": False}

            if self.awaiting_amount_after_show_limit:
                txt = l
                if any(k in txt for k in ("não", "nao", "n")):
                    self.awaiting_amount_after_show_limit = False
                    self.state = "ask_more"
                    return {"assistant": generate_message("ask_more"), "done": False}
                if amount is not None:
                    res = self.credit_agent.solicitar_aumento(self.cpf, float(amount))
                    self.awaiting_amount_after_show_limit = False
                    if not res.get("ok"):
                        return {"assistant": "Não foi possível processar sua solicitação. " + generate_message("ask_more"), "done": False}
                    status = res.get("status_pedido")
                    reason = res.get("reason", "")
                    name = self.authenticated_name or "cliente"
                    self.last_action = "credit"
                    if status == "aprovado":
                        self.state = "ask_more"
                        return {"assistant": f"Solicitação aprovada. {reason} Obrigado, {name}. " + generate_message("ask_more"), "done": False}
                    self.state = "offer_interview"
                    return {"assistant": f"Solicitação rejeitada. {reason} Deseja ser encaminhado para a entrevista de crédito para tentar reajustar seu score? (responda sim ou não)", "done": False}
                return {"assistant": "Se deseja solicitar aumento, informe o novo valor (apenas números) ou responda 'não' para encerrar.", "done": False}

            if amount is None:
                return {"assistant": generate_message("ask_credit_amount", user_message="Não entendi o valor. Por favor informe apenas números, ex.: 5000 ou 1500.50."), "done": False}

            res = self.credit_agent.solicitar_aumento(self.cpf, float(amount))
            if not res.get("ok"):
                return {"assistant": "Não foi possível processar sua solicitação. " + generate_message("ask_more"), "done": False}
            status = res.get("status_pedido")
            reason = res.get("reason", "")
            name = self.authenticated_name or "cliente"
            self.last_action = "credit"
            if status == "aprovado":
                self.state = "ask_more"
                return {"assistant": f"Solicitação aprovada. {reason} Obrigado, {name}. " + generate_message("ask_more"), "done": False}
            self.state = "offer_interview"
            return {"assistant": f"Solicitação rejeitada. {reason} Deseja ser encaminhado para a entrevista de crédito para tentar reajustar seu score? (responda sim ou não)", "done": False}

        # ---------------- EXCHANGE FLOW ----------------
        if self.state == "exchange_ask_currency":
            base, target = self._parse_exchange_text(text)
            self._clear_transient_flags()
            res = self.exchange_agent.get_rate(base=base, target=target)
            if not res.get("ok"):
                self.last_action = "exchange"
                self.state = "ask_more"
                return {"assistant": f"{res.get('msg')} {generate_message('ask_more')}", "done": False}
            rate = res.get("rate")
            rate_short = f"{rate:.2f}"
            self.last_action = "exchange"
            self.state = "ask_more"
            return {"assistant": f"Cotação atual: 1 {res.get('base')} = {rate_short} {res.get('target')}. {generate_message('ask_more')}", "done": False}

        # ---------------- INTERVIEW RUNNING ----------------
        if self.state == "interview_running":
            out = self.interview_agent.handle(text)
            assistant_text = out.get("assistant", "Desculpe, ocorreu um erro na entrevista.")
            done_flag = out.get("done", False)
            redirect = out.get("redirect")
            if done_flag:
                # se a entrevista solicitou redirecionamento a crédito, abrimos o fluxo de crédito
                if redirect == "credit":
                    self.state = "ask_credit_action"
                    self.last_action = "credit"
                    # usamos o fallback de ask_credit_action (que agora tem o texto correto)
                    follow = "Agora irei abrir as opções de crédito para nova análise."
                    return {"assistant": f"{assistant_text} {follow} {generate_message('ask_credit_action')}", "done": False}
                self.state = "post_auth"
                return {"assistant": f"{assistant_text} {self._build_post_auth_message(self.authenticated_name or 'cliente')}", "done": False}
            return {"assistant": assistant_text, "done": False}

        # ---------------- OFFER_INTERVIEW ----------------
        if self.state == "offer_interview":
            txt = l
            if any(k in txt for k in ("sim", "s", "quero", "ok", "yes")):
                if self.available_agents.get("interview"):
                    if not self.authenticated or not self.cpf:
                        return {"assistant": "Preciso autenticar sua conta primeiro. Por favor informe seu CPF (somente números).", "done": False}
                    start_msg = self.interview_agent.start(self.cpf)
                    self.state = "interview_running"
                    return {"assistant": start_msg, "done": False}
                self.state = "ask_more"
                return {"assistant": "No momento a entrevista de crédito não está implementada. " + generate_message("ask_more"), "done": False}
            if any(k in txt for k in ("não", "nao", "n")):
                self.state = "ask_more"
                return {"assistant": generate_message("ask_more"), "done": False}
            return {"assistant": "Não entendi. Deseja seguir para a entrevista de crédito? Responda sim ou não.", "done": False}

        # ---------------- ASK_MORE (robusto) ----------------
        if self.state == "ask_more":
            txt = l
            if any(k in txt for k in ("não", "nao", "n", "não, obrigado", "nao, obrigado")):
                self.state = "final"
                return {"assistant": "Obrigado por usar o Banco Ágil. Tenha um bom dia!", "done": True}

            if any(k in txt for k in ("menu", "opções", "opcoes", "ver menu", "mostrar opções", "mostrar opcoes")):
                self.state = "post_auth"
                return {"assistant": self._build_post_auth_message(self.authenticated_name or "cliente"), "done": False}

            if re.search(r"\b(consult(ar)?|cot(ação|acao)|cotar|moeda|outra|outras|outra(s)?|mais|continuar|nova|novo)\b", txt):
                if self.last_action == "exchange":
                    self.state = "exchange_ask_currency"
                    return {"assistant": generate_message("ask_exchange"), "done": False}
                if self.last_action == "credit":
                    self.state = "ask_credit_action"
                    return {"assistant": generate_message("ask_credit_action"), "done": False}
                return {"assistant": "Você quer continuar no mesmo contexto (ex.: consultar outra cotação) ou ver o menu principal? Responda 'continuar' ou 'menu'.", "done": False}

            if any(k in txt for k in ("sim", "s", "yes", "ok", "claro")):
                if self.last_action == "exchange":
                    return {"assistant": "Deseja consultar outra moeda (responda 'cotação' ou 'moeda') ou ver o menu principal (responda 'menu')?", "done": False}
                if self.last_action == "credit":
                    return {"assistant": "Deseja consultar crédito novamente ('consultar') ou solicitar novo aumento ('solicitar')? Ou escreva 'menu' para ver outras opções.", "done": False}
                self.state = "post_auth"
                return {"assistant": self._build_post_auth_message(self.authenticated_name or "cliente"), "done": False}

            return {"assistant": "Por favor responda 'sim' ou 'não'. Se quiser continuar no contexto atual (ex.: cotação), escreva 'cotação' ou 'consultar'. Se quiser ver o menu, escreva 'menu'.", "done": False}

        # ---------------- FALLBACK / FINAL ----------------
        return {"assistant": generate_message("action_result", sanitized_result={"action": "none", "result_text": "Atendimento finalizado."}), "done": True}

    # ---------------------------
    # Mensagens auxiliares
    # ---------------------------
    def _make_retry_message(self, remaining: int) -> str:
        """Mensagem quando a autenticação falha e há tentativas restantes."""
        return f"Não autenticado — restam {remaining} tentativa{'s' if remaining != 1 else ''}. Verifique o formato DD/MM/AAAA (ex.: 07/07/1985). Por favor, informe novamente seu CPF (somente números, sem pontos ou traços)."

    def _build_post_auth_message(self, name: str) -> str:
        """
        Monta a mensagem de menu principal após autenticação.
        Observação: corrigi o texto para listar apenas as ações que existem no Agente de Crédito.
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
