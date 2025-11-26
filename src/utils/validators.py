"""
Funções utilitárias de validação e extração de CPF e datas.

Este módulo centraliza a lógica de:
- normalizar CPFs (remover caracteres não numéricos),
- extrair um CPF válido (11 dígitos) de um texto,
- normalizar datas para um formato padrão (YYYY-MM-DD),
- extrair datas de textos livres em formatos comuns.

A ideia é manter essa parte isolada, para facilitar manutenção e evitar
duplicação de regex e parsing em vários pontos do sistema.
"""

from datetime import datetime
import re

# Regex básica para CPF com 11 dígitos (sem máscara)
CPF_RE = re.compile(r"\d{11}")


# ---------------- CPF helpers ----------------
def normalize_cpf(raw: str) -> str:
    """
    Recebe um CPF em qualquer formato (com pontos, traços, espaços etc.)
    e retorna apenas os dígitos.  
    Ex.: "123.456.789-01" -> "12345678901".
    """
    if not raw:
        return ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits


def extract_cpf(text: str) -> str:
    """
    Tenta encontrar um CPF (11 dígitos) dentro de um texto qualquer.
    - Junta todos os dígitos do texto e procura uma sequência de 11 dígitos.
    - Se encontrar, retorna essa sequência; caso contrário, retorna string vazia.
    """
    if not text:
        return ""
    digits = "".join(ch for ch in text if ch.isdigit())
    m = re.search(r"\d{11}", digits)
    return m.group(0) if m else ""


# ---------------- Date helpers ----------------
def normalize_date(raw: str) -> str:
    """
    Normaliza uma data em texto para o formato ISO 'YYYY-MM-DD'.

    Aceita formatos comuns:
    - DD/MM/YYYY
    - YYYY-MM-DD
    - DD-MM-YYYY
    - YYYY/MM/DD

    Se não encaixar nesses formatos, tenta um fallback básico procurando
    padrão dd mm yyyy dentro da string. Se não conseguir, retorna "".
    """
    if not raw:
        return ""
    s = raw.strip()

    # Tentativas com formatos mais comuns
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            continue

    # Fallback: tenta extrair algo como dd <sep> mm <sep> yyyy
    m = re.search(r"(\d{2})\D+(\d{2})\D+(\d{4})", s)
    if m:
        d, mo, y = m.groups()
        try:
            dt = datetime(int(y), int(mo), int(d))
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return ""
    return ""


def extract_date(text: str) -> str:
    """
    Tenta localizar uma data dentro de um texto e já devolve no formato normalizado.

    Fluxo:
    - procura primeiro por padrões explícitos (dd/mm/yyyy, yyyy-mm-dd, etc.);
    - se encontrar, normaliza via normalize_date;
    - se não encontrar nada, passa o texto inteiro para normalize_date como fallback.

    Em caso de falha, retorna string vazia.
    """
    if not text:
        return ""
    patterns = [
        r"\d{2}/\d{2}/\d{4}",
        r"\d{4}-\d{2}-\d{2}",
        r"\d{2}-\d{2}-\d{4}",
        r"\d{2}/\d{2}/\d{2,4}"
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return normalize_date(m.group(0))
    # fallback: tenta interpretar o texto inteiro como data
    return normalize_date(text)
