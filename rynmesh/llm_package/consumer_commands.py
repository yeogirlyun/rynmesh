"""Internal access to the existing consumer order lifecycle, without HTTP."""
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ConsumerCommands:
    submit: Callable[[dict], dict]
    status: Callable[[str], dict]
    cancel: Callable[[str], dict]
    acknowledge: Callable[[str], None]
