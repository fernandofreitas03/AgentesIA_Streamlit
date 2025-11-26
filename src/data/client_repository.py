# src/data/client_repository.py
from pathlib import Path
from typing import Optional, Dict
import pandas as pd
import logging

from ..utils.validators import normalize_cpf, normalize_date

logger = logging.getLogger(__name__)

class ClientRepository:
    """
    CSV-backed repository: columns expected include cpf and data_nascimento (or similar).
    """

    def __init__(self, csv_path: Path):
        self.csv_path = csv_path
        self._df = None
        self._load()

    def _load(self):
        if not self.csv_path.exists():
            logger.warning("clients csv not found at %s", self.csv_path)
            self._df = pd.DataFrame(columns=["cpf", "nome", "data_nascimento"])
            return
        self._df = pd.read_csv(self.csv_path, dtype=str)
        self._df.columns = [c.strip().lower() for c in self._df.columns]

    def find_by_cpf_and_dob(self, cpf_raw: str, dob_raw: str) -> Optional[Dict[str, str]]:
        """
        Return candidate dict if cpf and dob match; else None.
        Normalizes inputs.
        """
        if self._df is None:
            self._load()
        if self._df.empty:
            return None

        cpf = normalize_cpf(cpf_raw)
        dob = normalize_date(dob_raw)
        if not cpf or not dob:
            return None

        cols = self._df.columns.tolist()
        cpf_col = next((c for c in cols if "cpf" in c), None)
        dob_col = next((c for c in cols if "data" in c and "nasc" in c) or (c for c in cols if "dob" in c), None)
        name_col = next((c for c in cols if "nome" in c or "name" in c), None)

        if cpf_col is None or dob_col is None:
            logger.warning("CSV missing cpf or dob columns")
            return None

        df = self._df.copy()
        df[cpf_col] = df[cpf_col].astype(str).apply(normalize_cpf)
        df[dob_col] = df[dob_col].astype(str).apply(normalize_date)

        matched = df[(df[cpf_col] == cpf) & (df[dob_col] == dob)]
        if matched.empty:
            return None

        row = matched.iloc[0]
        return {
            "cpf": row.get(cpf_col, ""),
            "data_nascimento": row.get(dob_col, ""),
            "nome": row.get(name_col, "") if name_col else ""
        }
