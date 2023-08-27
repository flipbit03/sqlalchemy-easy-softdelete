from dataclasses import dataclass
from typing import Optional

from sqlalchemy import Table


@dataclass
class IgnoredTable:
    table_schema: Optional[str]
    name: str

    def match_name(self, table: Table):
        # Table matches if the name and schema match
        return self.name == table.name and self.table_schema == table.schema
