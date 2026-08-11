from pathlib import Path
from pydantic_settings import BaseSettings

PROJECT_ROOT = Path(__file__).parent.resolve()

class Settings(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 8100
    output_dir: str = ""
    max_concurrent: int = 2
    preferred_audio_format: str = "mp3"
    preferred_bitrate: str = "320k"

    model_config = {
        "env_prefix": "GRABBY_",
        "env_file": str(PROJECT_ROOT / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    @property
    def output_path(self) -> Path:
        p = Path(self.output_dir or str(PROJECT_ROOT / "output")).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

settings = Settings()
