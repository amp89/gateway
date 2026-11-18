"""Master key (passphrase -> scrypt -> Fernet), held in memory only; admin password hashing."""
import base64
import hashlib
import hmac
import json
import secrets

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

VERIFY_CONSTANT = b"gateway-verify-v1"
_MAXMEM = 512 * 1024 * 1024


class SealedError(Exception):
    pass


def derive_fernet(passphrase: str, salt: bytes, n: int, r: int = 8, p: int = 1) -> Fernet:
    raw = hashlib.scrypt(passphrase.encode(), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=_MAXMEM)
    return Fernet(base64.urlsafe_b64encode(raw))


def new_kdf(passphrase: str, n: int) -> tuple[Fernet, dict[str, str]]:
    """Fresh salt + key; returns the meta rows to store (never the key)."""
    salt = secrets.token_bytes(16)
    f = derive_fernet(passphrase, salt, n)
    meta = {
        "kdf_salt": base64.b64encode(salt).decode(),
        "kdf_params": json.dumps({"n": n, "r": 8, "p": 1}),
        "verify_token": f.encrypt(VERIFY_CONSTANT).decode(),
    }
    return f, meta


def check_passphrase(passphrase: str, salt_b64: str, params_json: str, verify_token: str) -> Fernet | None:
    params = json.loads(params_json)
    f = derive_fernet(passphrase, base64.b64decode(salt_b64), params["n"], params["r"], params["p"])
    try:
        ok = f.decrypt(verify_token.encode()) == VERIFY_CONSTANT
    except InvalidToken:
        return None
    return f if ok else None


class Vault:
    """Holds the Fernet key while unsealed. The key exists nowhere else."""

    def __init__(self) -> None:
        self._f: Fernet | MultiFernet | None = None

    @property
    def unsealed(self) -> bool:
        return self._f is not None

    def unseal(self, f: Fernet | MultiFernet) -> None:
        """A MultiFernet encrypts with its first key and decrypts with any (used after a passphrase change)."""
        self._f = f

    def seal(self) -> None:
        self._f = None

    def _key(self) -> Fernet | MultiFernet:
        if self._f is None:
            raise SealedError()
        return self._f

    def enc(self, plaintext: str) -> str:
        return self._key().encrypt(plaintext.encode()).decode()

    def dec(self, token: str) -> str:
        return self._key().decrypt(token.encode()).decode()

    def enc_json(self, value) -> str:
        return self.enc(json.dumps(value, separators=(",", ":")))

    def dec_json(self, token: str):
        return json.loads(self.dec(token))


def hash_password(password: str, n: int) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=n, r=8, p=1, dklen=32, maxmem=_MAXMEM)
    return f"scrypt${n}$8$1${base64.b64encode(salt).decode()}${base64.b64encode(h).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, h = stored.split("$")
        got = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32, maxmem=_MAXMEM
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, base64.b64decode(h))


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# Encrypted columns, re-encrypted on passphrase change: (table, primary key, columns). Tables added by later
# migrations register here; tables that do not exist yet are skipped.
ENCRYPTED_COLUMNS: list[tuple[str, str, tuple[str, ...]]] = [
    ("bots", "id", ("token_enc", "allowlist_enc")),
    ("services", "id", ("credential_enc",)),
    ("messages", "id", ("sender_enc", "body_enc", "chat_id_enc")),
    ("audit", "id", ("resource_enc",)),
    ("commands", "id", ("call_enc", "reply_enc", "last_error_enc")),
    ("permissions", "id", ("scope_enc",)),
]

# Encrypted meta values (key -> Fernet token), re-encrypted on passphrase change.
ENCRYPTED_META: tuple[str, ...] = ("telegram_users_enc",)
