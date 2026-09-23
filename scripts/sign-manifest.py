#!/usr/bin/env python3
"""Sign `dataset.meta.json` so a consumer can tell this dataset from a substituted one (oxyc/den#127).

    sign-manifest.py sign <meta.json>      # write `signature` into the meta
    sign-manifest.py payload <meta.json>   # print the exact bytes `sign` signs
    sign-manifest.py public-key            # print the key's public half: ed25519:<base64 of 32 raw bytes>
    sign-manifest.py keygen <path>         # write a new private key (PKCS#8 PEM, mode 0600)

The key is `$DEN_DATASET_SIGNING_KEY`, else `~/.config/den/dataset-signing.pem`: an Ed25519 private key in
PKCS#8 PEM. This script only ever passes its PATH to openssl; it never reads the key file.

## The payload: `den.dataset.v2`

    den.dataset.v2
    datasetVersion=<datasetVersion>
    taxonomyVersion=<taxonomyVersion>
    <key>=<value>        one line per top-level meta key ending in `Sha256`, sorted by key

joined by "\\n", no trailing newline. The meta's `signature` is `ed25519:` + base64 of the 64-byte signature.

Not the app's `den.dataset.v1` (`DatasetSignature.signingPayload` in oxyc/den). v1 signs five blob
checksums the release no longer carries and NOT `storeSha256` — and the store is now the whole dataset, so
a v1 signature could not tell a swapped store from the real one. v2 signs every checksum the meta declares,
whatever it is called, so a blob added later is covered without changing the format. The `field=` tags and
the domain line do the same job they do in v1: two adjacent values cannot slide past each other, and a
signature over some other Den message cannot be replayed as this one.

## The pinned key

`sign` refuses a key whose public half is not `PUBLIC_KEY` (`$DEN_DATASET_PUBLIC_KEY` overrides it, for
tests). The verifier pins that key; a meta signed with any other key would be refused by every consumer, so
it is refused here first, before anything uploads.

## openssl

macOS's `/usr/bin/openssl` is LibreSSL, which has no Ed25519. The first openssl that lists ED25519 among
its public-key algorithms is used: each `openssl` on PATH, then Homebrew's openssl@3, then
`nix run nixpkgs#openssl --`. None working is a refusal that says how to get one.
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile

DEFAULT_KEY = "~/.config/den/dataset-signing.pem"
PREFIX = "ed25519:"
DOMAIN = "den.dataset.v2"
# The public half of the dataset signing key — what the verifier pins.
PUBLIC_KEY = "ed25519:GEStq5EyXnzF3Zy9IgY8ATGE6mtQy30w4AQGv/3UtCo="

# RFC 8410 PKCS#8 wrapping of a 32-byte Ed25519 seed: SEQUENCE { version 0, AlgorithmIdentifier
# id-Ed25519, OCTET STRING { OCTET STRING seed } }. The seed follows these 16 bytes.
PKCS8_ED25519_PREFIX = bytes.fromhex("302e020100300506032b657004220420")

HOMEBREW = ("/opt/homebrew/opt/openssl@3/bin/openssl", "/usr/local/opt/openssl@3/bin/openssl")
NIX = ["nix", "run", "nixpkgs#openssl", "--"]


class SignError(Exception):
    pass


def key_path():
    return os.path.expanduser(os.environ.get("DEN_DATASET_SIGNING_KEY") or DEFAULT_KEY)


def payload(meta):
    """The `den.dataset.v2` bytes for `meta`."""
    def text(key):
        value = meta.get(key)
        if not isinstance(value, str) or not value:
            raise SignError(f"dataset.meta.json has no usable '{key}' (a non-empty string) to sign")
        if "\n" in value:
            raise SignError(f"dataset.meta.json '{key}' contains a newline, which would forge a payload line")
        return value

    lines = [DOMAIN, f"datasetVersion={text('datasetVersion')}", f"taxonomyVersion={text('taxonomyVersion')}"]
    checksums = sorted(key for key in meta if key.endswith("Sha256"))
    if not checksums:
        raise SignError("dataset.meta.json declares no *Sha256 — there is nothing for a signature to pin")
    lines += [f"{key}={text(key)}" for key in checksums]
    return "\n".join(lines).encode("utf-8")


def _candidates():
    seen = set()
    paths = [os.path.join(d, "openssl") for d in os.environ.get("PATH", "").split(os.pathsep)] + list(HOMEBREW)
    for path in paths:
        if os.access(path, os.X_OK) and os.path.realpath(path) not in seen:
            seen.add(os.path.realpath(path))
            yield [path]
    if shutil.which("nix"):
        yield NIX


def openssl():
    """The argv prefix of an openssl that can do Ed25519."""
    tried = []
    for command in _candidates():
        probe = subprocess.run(command + ["list", "-public-key-algorithms"], capture_output=True, text=True)
        if probe.returncode == 0 and "ED25519" in probe.stdout.upper():
            return command
        tried.append(" ".join(command))
    raise SignError("no openssl with Ed25519 found (tried: " + (", ".join(tried) or "none") + "). macOS's "
                    "/usr/bin/openssl is LibreSSL and cannot sign Ed25519. Install OpenSSL 3 "
                    "(`brew install openssl@3`, or `nix profile install nixpkgs#openssl`) or make `nix` "
                    "available, then publish again.")


def public_key(key):
    """`ed25519:` + base64 of the 32 raw public-key bytes of the private key at `key`."""
    if not os.path.isfile(key):
        raise SignError(f"no signing key at {key}")
    done = subprocess.run(openssl() + ["pkey", "-in", key, "-pubout", "-outform", "DER"], capture_output=True)
    if done.returncode != 0:
        raise SignError(f"openssl could not read {key}: {done.stderr.decode(errors='replace').strip()}")
    # SubjectPublicKeyInfo for Ed25519 is a fixed 12-byte header and the 32 raw bytes.
    if len(done.stdout) != 44:
        raise SignError(f"{key} is not an Ed25519 key")
    return PREFIX + base64.b64encode(done.stdout[-32:]).decode("ascii")


def sign_bytes(data, key):
    """Raw 64-byte Ed25519 signature over `data` with the private key at `key`."""
    if not os.path.isfile(key):
        raise SignError(f"no signing key at {key}")
    with tempfile.NamedTemporaryFile() as message:
        message.write(data)
        message.flush()
        done = subprocess.run(openssl() + ["pkeyutl", "-sign", "-rawin", "-inkey", key, "-in", message.name],
                              capture_output=True)
    if done.returncode != 0:
        raise SignError(f"openssl could not sign with {key}: {done.stderr.decode(errors='replace').strip()}")
    if len(done.stdout) != 64:
        raise SignError(f"openssl returned a {len(done.stdout)}-byte signature; Ed25519 signatures are 64 bytes")
    return done.stdout


def sign(meta_path, key):
    expected = os.environ.get("DEN_DATASET_PUBLIC_KEY") or PUBLIC_KEY
    actual = public_key(key)
    if actual != expected:
        raise SignError(f"{key} is not the dataset signing key: its public half is {actual}, and consumers "
                        f"pin {expected}. A meta signed with it would be refused everywhere.")
    with open(meta_path, encoding="utf-8") as fh:
        meta = json.load(fh)
    meta["signature"] = PREFIX + base64.b64encode(sign_bytes(payload(meta), key)).decode("ascii")
    # Rewritten the way prune-manifest.py writes it; key order and layout are not part of what is signed.
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=1)
        fh.write("\n")
    return meta["signature"]


def pem(seed):
    """The PKCS#8 PEM openssl reads for the 32-byte Ed25519 seed `seed` (CryptoKit's `rawRepresentation`)."""
    der = PKCS8_ED25519_PREFIX + seed
    return "-----BEGIN PRIVATE KEY-----\n" + base64.b64encode(der).decode("ascii") + "\n-----END PRIVATE KEY-----\n"


def keygen(path):
    """A fresh Ed25519 private key at `path`. Any 32 random bytes are a valid Ed25519 seed, so no openssl."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(pem(os.urandom(32)))


def main(argv):
    usage = "usage: sign-manifest.py sign <meta.json> | payload <meta.json> | public-key | keygen <path>"
    try:
        if len(argv) == 2 and argv[0] == "sign":
            print(f"signed {argv[1]} with {key_path()}: {sign(argv[1], key_path())}")
        elif len(argv) == 2 and argv[0] == "payload":
            with open(argv[1], encoding="utf-8") as fh:
                sys.stdout.write(payload(json.load(fh)).decode("utf-8") + "\n")
        elif len(argv) == 1 and argv[0] == "public-key":
            print(public_key(key_path()))
        elif len(argv) == 2 and argv[0] == "keygen":
            keygen(argv[1])
            print(f"wrote {argv[1]}")
        else:
            print(usage, file=sys.stderr)
            return 2
    except SignError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
