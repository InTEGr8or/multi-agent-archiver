from dataclasses import dataclass
from typing import Optional

from rich import box as rich_box


@dataclass(frozen=True)
class Theme:
    """Visual theme for terminal output (tables, panels)."""

    table_box: Optional[rich_box.Box] = None
    panel_box: rich_box.Box = rich_box.MINIMAL
    table_padding: tuple[int, int, int, int] = (0, 2, 0, 0)
    header_style: str = "on grey23"


DEFAULT = Theme()
