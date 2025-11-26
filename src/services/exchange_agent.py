# src/services/exchange_agent.py
"""
Agente de Câmbio (ExchangeAgent)

Este módulo encapsula toda a lógica necessária para consultar a cotação
de moedas em tempo real usando a API da apilayer (exchangerates_data).
A função principal é o método `get_rate`, que recebe moedas base/target
e devolve a cotação formatada.  

O agente foi escrito para ser tolerante a falhas — caso a API retorne erro,
a chave esteja incorreta ou haja instabilidade de rede, o retorno será
sempre uma mensagem amigável para o usuário, sem expor detalhes técnicos
ou mensagens sensíveis.
"""

from typing import Dict, Any
from datetime import datetime
import requests
from decouple import config
import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# -------------------------------------------------------------------
# URL base da API de câmbio da apilayer. O endpoint retorna o preço
# mais recente para a moeda base, com a lista de símbolos desejados.
# -------------------------------------------------------------------
API_URL = "https://api.apilayer.com/exchangerates_data/latest"


# -------------------------------------------------------------------
# Funções auxiliares simples para padronizar timestamp e leitura da key
# -------------------------------------------------------------------
def _now_iso() -> str:
    """Retorna timestamp ISO no padrão UTC, usado para logs e metadata."""
    return datetime.utcnow().isoformat() + "Z"


def _get_api_key() -> str:
    """
    Recupera a API key do ambiente (.env via decouple) ou, se disponível,
    do Streamlit secrets.  
    Esse método nunca levanta exceções — se nada for encontrado,
    retorna None e o agente utiliza fallback amigável.
    """
    key = config("EXCHANGE_API_KEY", default=None)
    if key:
        return key

    # tentativa com st.secrets caso Streamlit esteja disponível
    try:
        import streamlit as st
        key = st.secrets.get("EXCHANGE_API_KEY") if hasattr(st, "secrets") else None
        return key
    except Exception:
        return None


# -------------------------------------------------------------------
# Classe principal do agente de câmbio
# -------------------------------------------------------------------
class ExchangeAgent:
    """
    Agente responsável por consultar a cotação de moedas em tempo real.
    A classe abstrai todo o fluxo de requisição, tratamento de erros e
    normalização do retorno. A interface pública é apenas `get_rate`.
    """

    def __init__(self, timeout: float = 6.0):
        # Timeout de segurança para evitar travamentos em chamadas externas
        self.timeout = timeout
        self.api_url = API_URL

    # -------------------------------------------------------------------
    # Consulta de cotação
    # -------------------------------------------------------------------
    def get_rate(self, base: str = "USD", target: str = "BRL") -> Dict[str, Any]:
        """
        Consulta a API de câmbio usando a apilayer.

        Parâmetros:
        - base: moeda base da conversão (ex.: USD)
        - target: moeda de destino (ex.: BRL)

        Retorno:
        Um dicionário padronizado contendo:
        {
            "ok": bool,
            "base": str,
            "target": str,
            "rate": float|None,
            "timestamp": str,
            "msg": str
        }

        Em caso de falha, o retorno terá ok=False e uma mensagem neutra,
        sem detalhes internos ou códigos de erro da API.
        """
        base = (base or "USD").upper()
        target = (target or "BRL").upper()

        # Recupera chave da API
        api_key = _get_api_key()
        if not api_key:
            logger.warning("ExchangeAgent: API key not found (EXCHANGE_API_KEY).")
            return {
                "ok": False,
                "base": base,
                "target": target,
                "rate": None,
                "timestamp": _now_iso(),
                "msg": "Serviço indisponível. Volte mais tarde."
            }

        headers = {"apikey": api_key}
        params = {"base": base, "symbols": target}

        # Tentativa de chamada HTTP
        try:
            r = requests.get(self.api_url, headers=headers, params=params, timeout=self.timeout)
        except requests.RequestException as e:
            logger.exception("ExchangeAgent network error: %s", e)
            return {
                "ok": False,
                "base": base,
                "target": target,
                "rate": None,
                "timestamp": _now_iso(),
                "msg": "Serviço indisponível. Volte mais tarde."
            }

        # Verificação de status HTTP
        if r.status_code != 200:
            logger.warning("ExchangeAgent HTTP error %s: %s", r.status_code, r.text[:300])
            return {
                "ok": False,
                "base": base,
                "target": target,
                "rate": None,
                "timestamp": _now_iso(),
                "msg": "Serviço indisponível. Volte mais tarde."
            }

        # Tentativa de decodificar JSON
        try:
            payload = r.json()
        except Exception as e:
            logger.exception("ExchangeAgent invalid JSON: %s", e)
            return {
                "ok": False,
                "base": base,
                "target": target,
                "rate": None,
                "timestamp": _now_iso(),
                "msg": "Serviço indisponível. Volte mais tarde."
            }

        # A API da apilayer pode retornar success=False com detalhes no campo "error"
        if isinstance(payload, dict) and payload.get("success") is False:
            logger.warning("ExchangeAgent API returned success=False: %s", payload.get("error") or payload)
            return {
                "ok": False,
                "base": base,
                "target": target,
                "rate": None,
                "timestamp": _now_iso(),
                "msg": "Serviço indisponível. Volte mais tarde."
            }

        # Extração da taxa de câmbio
        try:
            rates = payload.get("rates", {})
            rate = rates.get(target)

            # fallback em caso de payload inesperado
            if rate is None:
                if "rate" in payload and isinstance(payload["rate"], (int, float)):
                    rate = float(payload["rate"])
                else:
                    logger.warning("ExchangeAgent: rate missing in payload: %s", payload)
                    return {
                        "ok": False,
                        "base": base,
                        "target": target,
                        "rate": None,
                        "timestamp": _now_iso(),
                        "msg": "Serviço indisponível. Volte mais tarde."
                    }

            return {
                "ok": True,
                "base": base,
                "target": target,
                "rate": float(rate),
                "timestamp": _now_iso(),
                "msg": f"Cotação obtida: 1 {base} = {float(rate):.6f} {target}."
            }

        except Exception as e:
            logger.exception("ExchangeAgent error parsing rate: %s", e)
            return {
                "ok": False,
                "base": base,
                "target": target,
                "rate": None,
                "timestamp": _now_iso(),
                "msg": "Serviço indisponível. Volte mais tarde."
            }
