"""Environment-driven configuration. NO secrets or environment-specific values live
in code — everything comes from env vars (see .env.example). Fill a local .env
(gitignored) or use AWS Secrets Manager. `settings.summary()` prints what's set
without printing secret VALUES.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional: load a local .env if python-dotenv is installed
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass

REPO_ROOT = Path(__file__).resolve().parents[2]


def _get(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    # --- AWS coordinates (fill via env; do NOT hardcode) ---
    aws_account_id: str | None = field(default_factory=lambda: _get("AWS_ACCOUNT_ID"))
    aws_region: str = field(default_factory=lambda: _get("AWS_REGION", "us-east-2"))
    studio_region: str = field(default_factory=lambda: _get("STUDIO_REGION", "us-east-1"))
    s3_bucket: str | None = field(default_factory=lambda: _get("S3_BUCKET"))
    s3_prefix: str = field(default_factory=lambda: _get("S3_PREFIX", "eia-nowcast"))
    glue_silver_db: str = field(default_factory=lambda: _get("GLUE_SILVER_DB", "aai540_silver"))
    glue_gold_db: str = field(default_factory=lambda: _get("GLUE_GOLD_DB", "aai540_gold"))

    # --- local paths ---
    data_dir: Path = field(default_factory=lambda: Path(_get("DATA_DIR", str(REPO_ROOT / "data"))))

    # --- optional source credentials (names only; values from env) ---
    pems_username: str | None = field(default_factory=lambda: _get("PEMS_USERNAME"))
    pems_password: str | None = field(default_factory=lambda: _get("PEMS_PASSWORD"))
    census_api_key: str | None = field(default_factory=lambda: _get("CENSUS_API_KEY"))
    noaa_ncei_token: str | None = field(default_factory=lambda: _get("NOAA_NCEI_TOKEN"))
    purpleair_api_key: str | None = field(default_factory=lambda: _get("PURPLEAIR_API_KEY"))
    seatgeek_client_id: str | None = field(default_factory=lambda: _get("SEATGEEK_CLIENT_ID"))

    def s3_uri(self, *parts: str) -> str:
        if not self.s3_bucket:
            raise RuntimeError("S3_BUCKET not set — see .env.example")
        key = "/".join([self.s3_prefix, *parts]).strip("/")
        return f"s3://{self.s3_bucket}/{key}"

    def summary(self) -> str:
        def mask(v: str | None) -> str:
            return "SET" if v else "unset"

        lines = [
            "EIA settings:",
            f"  aws_region        = {self.aws_region}",
            f"  studio_region     = {self.studio_region}",
            f"  aws_account_id    = {mask(self.aws_account_id)}",
            f"  s3_bucket         = {mask(self.s3_bucket)}  (prefix={self.s3_prefix})",
            f"  glue dbs          = {self.glue_silver_db} / {self.glue_gold_db}",
            f"  data_dir          = {self.data_dir}",
            "  credentials       = "
            + ", ".join(
                f"{k}:{mask(v)}"
                for k, v in {
                    "pems": self.pems_username and self.pems_password,
                    "census": self.census_api_key,
                    "noaa": self.noaa_ncei_token,
                    "purpleair": self.purpleair_api_key,
                    "seatgeek": self.seatgeek_client_id,
                }.items()
            ),
            "  (build-now sources — eSMR, Open-Meteo, BART, CDTFA, OI, TOT — need NONE of the above)",
        ]
        return "\n".join(lines)


settings = Settings()
