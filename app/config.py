"""Settings from the environment (see SPEC.md section 11)."""
import os
import re
from dataclasses import dataclass, replace

_PREFIX = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _list(value: str) -> tuple[str, ...]:
    return tuple(v.strip().lower() for v in value.split(",") if v.strip())


@dataclass(frozen=True)
class Settings:
    env: str = "development"
    api_prefix: str = "api"
    admin_prefix: str = "admin"
    api_hosts: tuple[str, ...] = ("localhost", "127.0.0.1")
    admin_hosts: tuple[str, ...] = ("localhost", "127.0.0.1")
    trusted_proxy_ips: tuple[str, ...] = ()  # IPs or CIDRs
    telegram_poll_timeout_s: int = 30
    upstream_timeout_s: float = 15.0
    upstream_max_bytes: int = 1_048_576
    api_rate_per_min: int = 120
    session_idle_min: int = 60
    audit_retention_days: int = 90
    default_message_retention_days: int = 30
    dev_auto_unlock_passphrase: str = ""
    dev_admin_password: str = "asdf"
    database_path: str = "/data/gateway.sqlite3"
    unlock_token_path: str = "/run/gateway/unlock.token"
    setup_code_path: str = "/run/gateway/setup.code"
    ui_dir: str = "/srv/ui"
    packs_dir: str = "/srv/packs"
    dev_api_port: int = 38081  # dev only: host port of the API, for the API URL shown in code samples
    # scrypt cost; tests lower it to keep the suite fast
    kdf_n: int = 2**17

    @property
    def production(self) -> bool:
        return self.env == "production"

    @property
    def api_url(self) -> str:
        """Client API base as callers elsewhere reach it: first API_HOSTS name + API_PREFIX.
        Prod: https (Cloudflare / Tailscale terminate TLS). Dev: http on the published dev port."""
        host = self.api_hosts[0] if self.api_hosts else "localhost"
        host = f"[{host}]" if ":" in host else host
        if self.production:
            return f"https://{host}/{self.api_prefix}/v1"
        return f"http://{host}:{self.dev_api_port}/{self.api_prefix}/v1"

    def with_(self, **kw) -> "Settings":
        return replace(self, **kw)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env
        d = cls()

        def g(name: str, default):
            return e.get(name, "").strip() or default

        return cls(
            env=g("ENV", d.env),
            api_prefix=g("API_PREFIX", d.api_prefix).strip("/"),
            admin_prefix=g("ADMIN_PREFIX", d.admin_prefix).strip("/"),
            api_hosts=_list(g("API_HOSTS", ",".join(d.api_hosts))),
            admin_hosts=_list(g("ADMIN_HOSTS", ",".join(d.admin_hosts))),
            trusted_proxy_ips=_list(g("TRUSTED_PROXY_IPS", "")),
            telegram_poll_timeout_s=int(g("TELEGRAM_POLL_TIMEOUT_S", d.telegram_poll_timeout_s)),
            upstream_timeout_s=float(g("UPSTREAM_TIMEOUT_S", d.upstream_timeout_s)),
            upstream_max_bytes=int(g("UPSTREAM_MAX_BYTES", d.upstream_max_bytes)),
            api_rate_per_min=int(g("API_RATE_PER_MIN", d.api_rate_per_min)),
            session_idle_min=int(g("SESSION_IDLE_MIN", d.session_idle_min)),
            audit_retention_days=int(g("AUDIT_RETENTION_DAYS", d.audit_retention_days)),
            default_message_retention_days=int(
                g("DEFAULT_MESSAGE_RETENTION_DAYS", d.default_message_retention_days)
            ),
            dev_auto_unlock_passphrase=e.get("DEV_AUTO_UNLOCK_PASSPHRASE", ""),
            dev_admin_password=g("DEV_ADMIN_PASSWORD", d.dev_admin_password),
            database_path=g("DATABASE_PATH", d.database_path),
            unlock_token_path=g("UNLOCK_TOKEN_PATH", d.unlock_token_path),
            setup_code_path=g("SETUP_CODE_PATH", d.setup_code_path),
            ui_dir=g("UI_DIR", d.ui_dir),
            packs_dir=g("PACKS_DIR", d.packs_dir),
            dev_api_port=int(g("GATEWAY_DEV_PORT", d.dev_api_port)),
        )

    def validate(self) -> None:
        for name, v in (("API_PREFIX", self.api_prefix), ("ADMIN_PREFIX", self.admin_prefix)):
            if not _PREFIX.fullmatch(v):
                raise RuntimeError(f"{name} must be one path segment: letters, digits, - or _ (got {v!r})")
        if self.api_prefix == self.admin_prefix:
            raise RuntimeError("API_PREFIX and ADMIN_PREFIX must differ")
        if self.production and self.dev_auto_unlock_passphrase:
            raise RuntimeError("DEV_AUTO_UNLOCK_PASSPHRASE is forbidden when ENV=production")
