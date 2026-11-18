"""Run inside the gateway container by unlock.sh: reads the passphrase from stdin, posts it to /_unlock."""
import json
import sys
import urllib.error
import urllib.request

from .config import Settings


def main() -> int:
    s = Settings.from_env()
    try:
        token = open(s.unlock_token_path).read().strip()
    except OSError:
        print("unlock token not found (is the gateway running?)", file=sys.stderr)
        return 1
    passphrase = sys.stdin.readline().rstrip("\n")
    req = urllib.request.Request(
        "http://127.0.0.1:8000/_unlock",
        data=json.dumps({"passphrase": passphrase}).encode(),
        headers={"Content-Type": "application/json", "X-Unlock-Token": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            print(json.load(r).get("state", "ok"))
            return 0
    except urllib.error.HTTPError as e:
        body = json.loads(e.read() or b"{}")
        msg = body.get("error", f"HTTP {e.code}")
        if body.get("retry_after"):
            msg += f" (retry in {body['retry_after']}s)"
        print(msg, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
