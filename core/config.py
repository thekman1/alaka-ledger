"""Explicit, private runtime configuration; importing this module performs no I/O."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


DATABASE_PATH = Path(__file__).resolve().parents[1] / ".local" / "alaka_vault.sqlite3"


class ConfigurationError(ValueError):
    """Indicate invalid configuration without disclosing its values."""


@dataclass(frozen=True)
class Settings:
    """Hold validated local settings with outbound traffic disabled by default."""

    database_path: Path
    allow_market_data: bool = False

    @classmethod
    def from_environment(cls) -> "Settings":
        """Load the working directory's .env without overriding process variables.

        Raises:
            ConfigurationError: The fixed path is not locally usable or the
                outbound market-data flag is not a boolean string.
        """
        load_dotenv(Path.cwd() / ".env", override=False)
        enabled = os.environ.get("ALAKA_ALLOW_MARKET_DATA", "false").strip().lower()
        if enabled not in {"true", "false"}:
            raise ConfigurationError("ALAKA_ALLOW_MARKET_DATA must be true or false.")
        path = DATABASE_PATH
        if path.is_dir() or (path.parent.exists() and not path.parent.is_dir()):
            raise ConfigurationError("The local database location is not usable.")
        if path.drive.startswith("\\\\") or str(path).startswith("//"):
            raise ConfigurationError("Network-share database paths are not supported.")
        return cls(database_path=path, allow_market_data=enabled == "true")