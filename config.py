from pathlib import Path
from pydantic_settings import BaseSettings

PROJECT_ROOT = Path(__file__).parent.resolve()

# Default: share DrivePod's cache directory
DEFAULT_CACHE_DIR = str(Path("/home/user/projects/drivepod/cache").resolve())

class Settings(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 8100
    output_dir: str = DEFAULT_CACHE_DIR
    max_concurrent: int = 2
    preferred_audio_format: str = "mp3"
    audio_bitrate: str = "160k"  # e.g. 128k, 192k, 256k, 320k
    audio_bitrate_mode: str = "cbr"  # cbr or vbr (vbr uses -q:a 2)
    audio_sample_rate: int = 44100  # 44.1 kHz (22050, 44100, 48000)
    audio_channels: int = 2  # 1 = mono, 2 = stereo

    # LLM duplicate detection
    llm_api_key: str = ""
    llm_base_url: str = ""

    # YouTube cookies (Netscape format)
    youtube_cookies: str = ""

    # User agent for yt-dlp
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"

    model_config = {
        "env_prefix": "GRABBY_",
        "env_file": str(PROJECT_ROOT / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    @property
    def output_path(self) -> Path:
        p = Path(self.output_dir or DEFAULT_CACHE_DIR).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

settings = Settings()
