from dataclasses import dataclass
from pathlib import Path

PUBLIC_PREFIX: str
STANDALONE_ENV_FILES: tuple[str, ...]
MAX_ENV_FILE_BYTES: int

def env_file_names(mode: str) -> tuple[str, ...]: ...
def load_env(root: str | Path = ..., mode: str = ...) -> LoadedEnv: ...
def public_env_vars(environ: dict[str, str] | None = ...) -> dict[str, str]: ...
def public_env_defines(environ: dict[str, str] | None = ...) -> list[str]: ...

@dataclass(frozen=True)
class LoadedEnv:
    root: Path
    mode: str
    files: tuple[str, ...]
    values: dict[str, str]
    applied: dict[str, str]
    @property
    def public_values(self) -> dict[str, str]: ...
