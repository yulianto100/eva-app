import logging
import os
import stat
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
LOGGER = logging.getLogger(__name__)


def env_flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes", "on"}


def load_local_env(base_dir: Path) -> None:
    env_path = base_dir / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


if env_flag("LOAD_LOCAL_ENV", "true"):
    load_local_env(BASE_DIR)


def resolve_config_path(base_dir: Path, env_name: str, default_path: Path, fallback_env_name: str | None = None) -> Path:
    raw_path = os.getenv(env_name)
    if raw_path is None and fallback_env_name:
        raw_path = os.getenv(fallback_env_name)
    path = Path(raw_path) if raw_path else base_dir / default_path
    return path if path.is_absolute() else base_dir / path


def read_config_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def read_secret_file(env_name: str, file_env_name: str | None = None, default: str = "") -> str:
    """
    Baca secret dari env var atau file (Docker secrets pattern).

    Priority: ENV_VAR > ENV_VAR_FILE content > default.
    """
    value = os.getenv(env_name, "").strip()
    if value:
        return value

    file_path = os.getenv(file_env_name or f"{env_name}_FILE", "").strip()
    if file_path:
        try:
            return Path(file_path).read_text(encoding="utf-8").strip()
        except (FileNotFoundError, PermissionError) as exc:
            LOGGER.warning("Gagal baca secret file %s: %s", file_path, exc)

    return default


def generate_secret_key(length: int = 64) -> str:
    """Generate cryptographically secure random key."""
    import secrets

    return secrets.token_urlsafe(length)


class Config:
    BASE_DIR = BASE_DIR
    UPLOAD_DIR = BASE_DIR / "uploads"
    OUTPUT_DIR = BASE_DIR / "outputs"
    REGION_DB_PATH = resolve_config_path(BASE_DIR, "REGION_DB_PATH", Path("data") / "region.db")
    ASTRI_DOWNLOAD_DIR = resolve_config_path(
        BASE_DIR,
        "ASTRI_DOWNLOAD_DIR",
        Path("static") / "downloads" / "astri_drm_apd_real_docs",
    )
    NETWORK_TYPES = ("AE", "UG")
    DEFAULT_NETWORK_TYPE = "AE"
    SCOPE_TYPES = ("Cluster", "Feeder", "Subfeeder")
    DEFAULT_SCOPE_TYPE = "Cluster"
    AUTO_BOUNDARY_BUFFER_METERS = float(os.getenv("AUTO_BOUNDARY_BUFFER_METERS", "50"))
    FEEDER_AUTO_BOUNDARY_BUFFER_METERS = float(os.getenv("FEEDER_AUTO_BOUNDARY_BUFFER_METERS", "80"))
    SUBFEEDER_AUTO_BOUNDARY_BUFFER_METERS = float(os.getenv("SUBFEEDER_AUTO_BOUNDARY_BUFFER_METERS", "80"))

    BOQ_TEMPLATE_PATH_AE = resolve_config_path(
        BASE_DIR,
        "BOQ_TEMPLATE_AE_PATH",
        Path("templates") / "BoQ APD Cluster_AE.xlsx",
        fallback_env_name="BOQ_TEMPLATE_PATH",
    )
    BOQ_TEMPLATE_PATH_UG = resolve_config_path(
        BASE_DIR,
        "BOQ_TEMPLATE_UG_PATH",
        Path("templates") / "BoQ APD Cluster_UG.xlsx",
    )
    BOQ_TEMPLATE_PATHS = {
        "AE": BOQ_TEMPLATE_PATH_AE,
        "UG": BOQ_TEMPLATE_PATH_UG,
    }
    BOQ_TEMPLATE_PATH = BOQ_TEMPLATE_PATH_AE
    _BOQ_OUTPUT_DIR = os.getenv("BOQ_OUTPUT_DIR")
    BOQ_OUTPUT_DIR = Path(_BOQ_OUTPUT_DIR) if _BOQ_OUTPUT_DIR else OUTPUT_DIR / "boq"
    if not BOQ_OUTPUT_DIR.is_absolute():
        BOQ_OUTPUT_DIR = BASE_DIR / BOQ_OUTPUT_DIR
    HPDB_TEMPLATE_PATH_AE = resolve_config_path(
        BASE_DIR,
        "HPDB_TEMPLATE_AE_PATH",
        Path("templates") / "TEMPLATE HPDB_AE.xlsx",
        fallback_env_name="HPDB_TEMPLATE_PATH",
    )
    HPDB_TEMPLATE_PATH_UG = resolve_config_path(
        BASE_DIR,
        "HPDB_TEMPLATE_UG_PATH",
        Path("templates") / "TEMPLATE HPDB_UG.xlsx",
    )
    HPDB_TEMPLATE_PATHS = {
        "AE": HPDB_TEMPLATE_PATH_AE,
        "UG": HPDB_TEMPLATE_PATH_UG,
    }
    HPDB_TEMPLATE_PATH = HPDB_TEMPLATE_PATH_AE
    _HPDB_OUTPUT_DIR = os.getenv("HPDB_OUTPUT_DIR")
    HPDB_OUTPUT_DIR = Path(_HPDB_OUTPUT_DIR) if _HPDB_OUTPUT_DIR else OUTPUT_DIR / "hpdb"
    if not HPDB_OUTPUT_DIR.is_absolute():
        HPDB_OUTPUT_DIR = BASE_DIR / HPDB_OUTPUT_DIR
    HPDB_DEFAULTS_PATH_AE = resolve_config_path(
        BASE_DIR,
        "HPDB_DEFAULTS_AE_PATH",
        Path("config") / "hpdb_defaults_AE.json",
        fallback_env_name="HPDB_DEFAULTS_PATH",
    )
    HPDB_DEFAULTS_PATH_UG = resolve_config_path(
        BASE_DIR,
        "HPDB_DEFAULTS_UG_PATH",
        Path("config") / "hpdb_defaults_UG.json",
    )
    HPDB_DEFAULTS_PATHS = {
        "AE": HPDB_DEFAULTS_PATH_AE,
        "UG": HPDB_DEFAULTS_PATH_UG,
    }
    HPDB_DEFAULTS_PATH = HPDB_DEFAULTS_PATH_AE
    DXF_TEMPLATE_PATH_AE = resolve_config_path(
        BASE_DIR,
        "DXF_TEMPLATE_AE_PATH",
        Path("dxf_templates") / "Template_AE.dxf",
        fallback_env_name="DXF_TEMPLATE_PATH",
    )
    DXF_TEMPLATE_PATH_UG = resolve_config_path(
        BASE_DIR,
        "DXF_TEMPLATE_UG_PATH",
        Path("dxf_templates") / "Template_UG.dxf",
    )
    DXF_TEMPLATE_PATHS = {
        "AE": DXF_TEMPLATE_PATH_AE,
        "UG": DXF_TEMPLATE_PATH_UG,
    }
    DXF_TEMPLATE_PATH = DXF_TEMPLATE_PATH_AE

    # Feeder DXF templates
    DXF_FEEDER_TEMPLATE_PATH_AE = resolve_config_path(
        BASE_DIR,
        "DXF_FEEDER_TEMPLATE_AE_PATH",
        Path("dxf_templates") / "Template_Feeder_AE.dxf",
    )
    DXF_FEEDER_TEMPLATE_PATH_UG = resolve_config_path(
        BASE_DIR,
        "DXF_FEEDER_TEMPLATE_UG_PATH",
        Path("dxf_templates") / "Template_Feeder_UG.dxf",
    )
    DXF_FEEDER_TEMPLATE_PATHS = {
        "AE": DXF_FEEDER_TEMPLATE_PATH_AE,
        "UG": DXF_FEEDER_TEMPLATE_PATH_UG,
    }

    # Feeder BOQ templates
    BOQ_FEEDER_TEMPLATE_PATH_AE = resolve_config_path(
        BASE_DIR,
        "BOQ_FEEDER_TEMPLATE_AE_PATH",
        Path("templates") / "BoQ APD Feeder_AE.xlsx",
    )
    BOQ_FEEDER_TEMPLATE_PATH_UG = resolve_config_path(
        BASE_DIR,
        "BOQ_FEEDER_TEMPLATE_UG_PATH",
        Path("templates") / "BoQ APD Feeder_UG.xlsx",
    )
    BOQ_FEEDER_TEMPLATE_PATHS = {
        "AE": BOQ_FEEDER_TEMPLATE_PATH_AE,
        "UG": BOQ_FEEDER_TEMPLATE_PATH_UG,
    }

    # Subfeeder templates start as copies of Feeder templates, so future
    # Subfeeder-specific rules can diverge without changing Feeder outputs.
    DXF_SUBFEEDER_TEMPLATE_PATH_AE = resolve_config_path(
        BASE_DIR,
        "DXF_SUBFEEDER_TEMPLATE_AE_PATH",
        Path("dxf_templates") / "Template_Subfeeder_AE.dxf",
    )
    DXF_SUBFEEDER_TEMPLATE_PATH_UG = resolve_config_path(
        BASE_DIR,
        "DXF_SUBFEEDER_TEMPLATE_UG_PATH",
        Path("dxf_templates") / "Template_Subfeeder_UG.dxf",
    )
    DXF_SUBFEEDER_TEMPLATE_PATHS = {
        "AE": DXF_SUBFEEDER_TEMPLATE_PATH_AE,
        "UG": DXF_SUBFEEDER_TEMPLATE_PATH_UG,
    }

    BOQ_SUBFEEDER_TEMPLATE_PATH_AE = resolve_config_path(
        BASE_DIR,
        "BOQ_SUBFEEDER_TEMPLATE_AE_PATH",
        Path("templates") / "BoQ APD Subfeeder_AE.xlsx",
    )
    BOQ_SUBFEEDER_TEMPLATE_PATH_UG = resolve_config_path(
        BASE_DIR,
        "BOQ_SUBFEEDER_TEMPLATE_UG_PATH",
        Path("templates") / "BoQ APD Subfeeder_UG.xlsx",
    )
    BOQ_SUBFEEDER_TEMPLATE_PATHS = {
        "AE": BOQ_SUBFEEDER_TEMPLATE_PATH_AE,
        "UG": BOQ_SUBFEEDER_TEMPLATE_PATH_UG,
    }

    # Combined template lookup by scope
    DXF_TEMPLATE_PATHS_BY_SCOPE = {
        "Cluster": DXF_TEMPLATE_PATHS,
        "Feeder": DXF_FEEDER_TEMPLATE_PATHS,
        "Subfeeder": DXF_SUBFEEDER_TEMPLATE_PATHS,
    }
    BOQ_TEMPLATE_PATHS_BY_SCOPE = {
        "Cluster": BOQ_TEMPLATE_PATHS,
        "Feeder": BOQ_FEEDER_TEMPLATE_PATHS,
        "Subfeeder": BOQ_SUBFEEDER_TEMPLATE_PATHS,
    }

    FDT_CODING_SPREADSHEET_URL_FILE = resolve_config_path(
        BASE_DIR,
        "FDT_CODING_SPREADSHEET_URL_FILE",
        Path("config") / "fdt_coding_url.txt",
    )
    FDT_CODING_SPREADSHEET_URL = (
        os.getenv("FDT_CODING_SPREADSHEET_URL", "").strip()
        or read_config_text(FDT_CODING_SPREADSHEET_URL_FILE)
    )
    _LOCAL_FDT_CODING_FALLBACK = Path.home() / "Downloads" / "Coding FTTH - Bekasi Area Update 2021 (1).xlsx"
    FDT_CODING_SPREADSHEET_PATH = resolve_config_path(
        BASE_DIR,
        "FDT_CODING_SPREADSHEET_PATH",
        _LOCAL_FDT_CODING_FALLBACK if _LOCAL_FDT_CODING_FALLBACK.exists() else Path("config") / "fdt_coding.xlsx",
    )
    FDT_CODING_DB_PATH = resolve_config_path(
        BASE_DIR,
        "FDT_CODING_DB_PATH",
        Path("data") / "fdt_coding.db",
    )
    COREMAN_DB_PATH = resolve_config_path(
        BASE_DIR,
        "COREMAN_DB_PATH",
        Path("data") / "coreman.db",
    )

    SECRET_KEY = read_secret_file("SECRET_KEY", default="change-me-in-production")
    SESSION_COOKIE_DOMAIN = ".eva-mrdesign.my.id"
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_SAMESITE = "Lax"

    MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "500"))
    MAX_BATCH_UPLOAD_MB = int(os.getenv("MAX_BATCH_UPLOAD_MB", "5000"))
    MAX_FILE_BYTES = MAX_UPLOAD_MB * 1024 * 1024
    MAX_CONTENT_LENGTH = MAX_BATCH_UPLOAD_MB * 1024 * 1024
    ALLOWED_EXTENSIONS = (".osm", ".pbf", ".osm.pbf", ".kml", ".kmz")

    CLEANUP_MAX_AGE_SECONDS = int(float(os.getenv("CLEANUP_MAX_AGE_HOURS", "24")) * 3600)
    ASTRI_DOWNLOAD_MAX_AGE_SECONDS = int(float(os.getenv("ASTRI_DOWNLOAD_MAX_AGE_HOURS", "12")) * 3600)
    CLEANUP_INTERVAL_SECONDS = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "3600"))
    JOB_MAX_AGE_SECONDS = int(float(os.getenv("JOB_MAX_AGE_HOURS", "24")) * 3600)
    JOB_STORE_TTL_SECONDS = int(os.getenv("JOB_STORE_TTL_SECONDS", str(JOB_MAX_AGE_SECONDS)))
    JOB_STORE_KEY_PREFIX = os.getenv("JOB_STORE_KEY_PREFIX", "eva")
    REDIS_URL = os.getenv("REDIS_URL", "").strip()
    CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", REDIS_URL).strip()
    CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", REDIS_URL).strip()
    CONVERSION_QUEUE_ENABLED = os.getenv("CONVERSION_QUEUE_ENABLED", "auto").lower()
    CONVERSION_MAX_CONCURRENT_JOBS = max(1, int(os.getenv("CONVERSION_MAX_CONCURRENT_JOBS", "1")))
    JOB_SLOT_TTL_SECONDS = max(60, int(os.getenv("JOB_SLOT_TTL_SECONDS", "1800")))
    JOB_SLOT_RETRY_SECONDS = max(1, int(os.getenv("JOB_SLOT_RETRY_SECONDS", "5")))
    JOB_MAX_RETRIES = int(os.getenv("JOB_MAX_RETRIES", "2"))
    JOB_RETRY_DELAY_SECONDS = int(os.getenv("JOB_RETRY_DELAY_SECONDS", "30"))
    JOB_STALE_THRESHOLD_SECONDS = int(os.getenv("JOB_STALE_THRESHOLD_SECONDS", "1800"))
    JOB_STALE_CHECK_INTERVAL_SECONDS = int(os.getenv("JOB_STALE_CHECK_INTERVAL_SECONDS", "300"))

    PREVIEW_FEATURE_LIMIT = int(os.getenv("PREVIEW_FEATURE_LIMIT", "2500"))

    ADMIN_API_TOKEN = read_secret_file("ADMIN_API_TOKEN", default="")
    REQUIRE_ADMIN_API_TOKEN = env_flag("REQUIRE_ADMIN_API_TOKEN", "false")
    ALLOW_THREAD_QUEUE_FALLBACK = env_flag("ALLOW_THREAD_QUEUE_FALLBACK", "true")
    ENFORCE_PRODUCTION_SECRETS = env_flag("ENFORCE_PRODUCTION_SECRETS", "false")

    HOST = os.getenv("HOST", "0.0.0.0")
    PORT = int(os.getenv("PORT", "5000"))
    DEBUG = os.getenv("FLASK_DEBUG", "false").lower() in {"1", "true", "yes"}
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
    LOG_FORMAT = os.getenv("LOG_FORMAT", "auto").lower()
    APP_VERSION = os.getenv("APP_VERSION", "1.1.2")
    SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()
    RELEASE_VERSION = os.getenv("RELEASE_VERSION", "unknown").strip() or "unknown"
    ENVIRONMENT = os.getenv("ENVIRONMENT", "production").strip() or "production"
    DXF_CONVERSION_ENABLED = os.getenv("DXF_CONVERSION_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
    HPDB_AUTO_GEOCODE_ENABLED = env_flag("HPDB_AUTO_GEOCODE_ENABLED", "true")
    GEOFABRIK_DIR = Path(os.getenv("GEOFABRIK_DIR", BASE_DIR / "geofabrik"))
    GEOFABRIK_KECAMATAN_DIR = Path(os.getenv("GEOFABRIK_KECAMATAN_DIR", GEOFABRIK_DIR / "kecamatan"))

    # --- Environment detection ---
    @property
    def is_staging(self) -> bool:
        return self.ENVIRONMENT == "staging"

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def is_development(self) -> bool:
        return self.ENVIRONMENT in {"development", "dev", "local"}

    # --- Staging-specific overrides ---
    # Staging uses less strict secret validation, more verbose logging,
    # and allows DXF conversion by default for testing.
    STAGING_LOG_LEVEL = os.getenv("STAGING_LOG_LEVEL", "DEBUG").upper()
    STAGING_DXF_ENABLED = env_flag("STAGING_DXF_ENABLED", "true")
    STAGING_SENTRY_TRACES_RATE = float(os.getenv("STAGING_SENTRY_TRACES_RATE", "0.5"))
    STAGING_CLEANUP_MAX_AGE_HOURS = int(os.getenv("STAGING_CLEANUP_MAX_AGE_HOURS", "6"))
    STAGING_MAX_UPLOAD_MB = int(os.getenv("STAGING_MAX_UPLOAD_MB", "200"))


def validate_production_secrets() -> list[str]:
    """
    Validasi bahwa secrets sudah di-set dengan benar di production.

    Return list warning/error yang bisa dilog saat startup.
    """
    warnings: list[str] = []

    is_staging = Config.ENVIRONMENT == "staging"
    is_debug = Config.DEBUG

    if Config.SECRET_KEY == "change-me-in-production" and not is_debug and not is_staging:
        warnings.append(
            "CRITICAL: SECRET_KEY masih default! "
            "Set SECRET_KEY environment variable dengan random string minimal 32 karakter."
        )

    if Config.SECRET_KEY and Config.SECRET_KEY != "change-me-in-production" and len(Config.SECRET_KEY) < 32:
        warnings.append("SECRET_KEY terlalu pendek. Gunakan random string minimal 32 karakter.")

    secrets_dir = Config.BASE_DIR / ".secrets"
    if secrets_dir.exists():
        try:
            mode = secrets_dir.stat().st_mode
            if mode & (stat.S_IROTH | stat.S_IWOTH | stat.S_IXOTH):
                warnings.append(".secrets directory world-readable/executable. Batasi permission ke owner/service user.")
        except OSError as exc:
            warnings.append(f"Gagal membaca permission .secrets: {exc}")

    return warnings
