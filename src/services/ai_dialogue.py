# src/services/ai_dialogue.py
"""
Mensagens de fallback e gerador simples de texto para o fluxo.
Comentários por bloco: explico o propósito do arquivo e das mensagens.
"""

import os
import logging
from decouple import config

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Tenta inicializar cliente OpenAI se a chave estiver configurada.
# Mantemos o uso do LLM opcional; o sistema funciona com mensagens fallback.
try:
    from openai import OpenAI
    key = config("OPENAI_API_KEY", default=None)
    if key:
        os.environ["OPENAI_API_KEY"] = key
    client = OpenAI()
except Exception as e:
    logger.debug("OpenAI client não inicializado: %s", e)
    client = None


# Mensagens simples e seguras usadas quando o LLM não é invocado.
FALLBACK = {
    "greeting": "Olá! Seja bem-vindo ao Banco Ágil. Para começarmos, por favor informe o seu CPF (somente números, sem pontos ou traços) para autenticarmos sua entrada.",
    "ask_cpf": "Por favor, informe seu CPF (somente números, sem pontos ou traços). Ex.: 12345678901.",
    "ask_dob": "Agora informe sua data de nascimento no formato DD/MM/AAAA. Ex.: 07/07/1985.",
    "ask_credit_action": "Escolha: (1) consultar seu limite atual ou (2) solicitar aumento de limite.",
    "ask_credit_amount": "Informe o novo limite desejado (apenas números). Ex.: 5000 ou 1500.50.",
    "ask_exchange": "Por favor informe a moeda e o sentido da cotação (ex.: 'USD para BRL' ou 'EUR').",
    "ask_more": "Posso ajudar em mais alguma coisa? Responda 'sim' ou 'não'.",
    "action_result": "Operação finalizada."
}


def generate_message(key: str, **kwargs) -> str:
    """
    Retorna a mensagem de fallback correspondente à chave fornecida.
    Em versões futuras poderíamos usar o LLM para variações seguras,
    mas sempre sem enviar PII.
    """
    return FALLBACK.get(key, "Desculpe, não entendi. Pode reformular?")
