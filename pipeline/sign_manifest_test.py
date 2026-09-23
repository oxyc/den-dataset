#!/usr/bin/env python3
"""`sign_manifest.py` — the `den.dataset.v2` payload, and signatures openssl agrees with."""
import base64
import importlib.util
import json
import os
import stat
import subprocess
import tempfile
import unittest
import unittest.mock

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("sign_manifest", os.path.join(HERE, "sign_manifest.py"))
sm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sm)

# A pruned, store-only meta as the publisher signs it (the shape of out-repass/dataset.meta.json).
META = {
    "datasetVersion": "5b1c3213b6a1",
    "taxonomyVersion": "t02",
    "embeddingModel": "bge-m3",
    "dims": 1024,
    "storeFile": "den-5b1c3213b6a1.store",
    "storeSha256": "c9" * 32,
    "storeRecords": 47618,
    "storeInputs": [{"arg": "corpus", "sha256": "ab" * 32}],
}

PAYLOAD = (b"den.dataset.v2\n"
           b"datasetVersion=5b1c3213b6a1\n"
           b"taxonomyVersion=t02\n"
           b"storeSha256=" + b"c9" * 32)

# RFC 8032 §7.1 TEST 2, a one-byte message (openssl's pkeyutl refuses TEST 1's empty one).
RFC_SEED = bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")
RFC_PUBLIC = bytes.fromhex("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c")
RFC_MESSAGE = bytes.fromhex("72")
RFC_SIGNATURE = bytes.fromhex("92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e"
                              "458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00")


class Payload(unittest.TestCase):
    def test_a_store_only_meta(self):
        """Nested checksums (`storeInputs[].sha256`) are the build's record, not a declared artifact."""
        self.assertEqual(sm.payload(META), PAYLOAD)

    def test_every_checksum_is_signed_sorted_by_key(self):
        meta = dict(META, labelsSha256="aa", facetsSha256="ff", premiseVectorsSha256="ee")
        self.assertEqual(sm.payload(meta), b"den.dataset.v2\ndatasetVersion=5b1c3213b6a1\ntaxonomyVersion=t02\n"
                                           b"facetsSha256=ff\nlabelsSha256=aa\npremiseVectorsSha256=ee\n"
                                           b"storeSha256=" + b"c9" * 32)

    def test_no_trailing_newline(self):
        self.assertFalse(sm.payload(META).endswith(b"\n"))

    def test_fields_outside_the_payload_do_not_change_it(self):
        self.assertEqual(sm.payload(dict(META, signature="ed25519:x", storeRebuild="why", dims=768)), PAYLOAD)

    def test_the_versions_and_a_checksum_are_required(self):
        for key in ("datasetVersion", "taxonomyVersion", "storeSha256"):
            with self.subTest(key=key), self.assertRaises(sm.SignError) as refused:
                sm.payload({k: v for k, v in META.items() if k != key})
            self.assertIn("Sha256" if key == "storeSha256" else key, str(refused.exception))

    def test_a_value_that_is_not_a_plain_string_is_refused(self):
        for bad in (None, 7, "", "c9\nstoreSha256=forged"):
            with self.subTest(value=bad), self.assertRaises(sm.SignError):
                sm.payload(dict(META, storeSha256=bad))


class Signing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.key = os.path.join(self.dir, "key.pem")
        sm.keygen(self.key)
        self.meta = os.path.join(self.dir, "dataset.meta.json")
        with open(self.meta, "w") as fh:
            json.dump(META, fh)
        pin = unittest.mock.patch.dict(os.environ, {"DEN_DATASET_PUBLIC_KEY": sm.public_key(self.key)})
        pin.start()
        self.addCleanup(pin.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def openssl_verifies(self, payload, signature, key):
        pub, msg, sig = (os.path.join(self.dir, n) for n in ("pub.pem", "msg", "sig"))
        subprocess.run(sm.openssl() + ["pkey", "-in", key, "-pubout", "-out", pub], check=True)
        with open(msg, "wb") as fh:
            fh.write(payload)
        with open(sig, "wb") as fh:
            fh.write(signature)
        done = subprocess.run(sm.openssl() + ["pkeyutl", "-verify", "-pubin", "-inkey", pub, "-rawin",
                                              "-in", msg, "-sigfile", sig], capture_output=True)
        return done.returncode == 0

    def test_the_pinned_key(self):
        self.assertEqual(sm.PUBLIC_KEY, "ed25519:GEStq5EyXnzF3Zy9IgY8ATGE6mtQy30w4AQGv/3UtCo=")

    def test_keygen_writes_a_private_file(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.key).st_mode), 0o600)
        with self.assertRaises(FileExistsError):
            sm.keygen(self.key)

    def test_the_signature_is_written_into_the_meta_and_openssl_verifies_it(self):
        signature = sm.sign(self.meta, self.key)
        with open(self.meta) as fh:
            self.assertEqual(json.load(fh)["signature"], signature)
        self.assertTrue(signature.startswith("ed25519:"))
        raw = base64.b64decode(signature[len("ed25519:"):])
        self.assertEqual(len(raw), 64)
        self.assertTrue(self.openssl_verifies(PAYLOAD, raw, self.key))
        # A swapped store is exactly what the signature must catch.
        self.assertFalse(self.openssl_verifies(PAYLOAD.replace(b"c9c9", b"c9c8", 1), raw, self.key))

    def test_signing_is_deterministic_and_replaces_an_old_signature(self):
        """openssl's Ed25519 is RFC 8032's deterministic one; the signature field is not in the payload."""
        first = sm.sign(self.meta, self.key)
        self.assertEqual(sm.sign(self.meta, self.key), first)

    def test_the_rfc_8032_vector(self):
        """The PKCS#8 wrapping and openssl together produce the standard Ed25519 signature — so a raw seed
        (CryptoKit's `rawRepresentation`) wrapped by `pem` is the same key everywhere."""
        key = os.path.join(self.dir, "rfc.pem")
        with open(key, "w") as fh:
            fh.write(sm.pem(RFC_SEED))
        self.assertEqual(sm.sign_bytes(RFC_MESSAGE, key), RFC_SIGNATURE)
        self.assertEqual(sm.public_key(key), "ed25519:" + base64.b64encode(RFC_PUBLIC).decode())

    def test_a_key_that_is_not_the_pinned_one_is_refused(self):
        with unittest.mock.patch.dict(os.environ, {"DEN_DATASET_PUBLIC_KEY": sm.PUBLIC_KEY}):
            with self.assertRaises(sm.SignError) as refused:
                sm.sign(self.meta, self.key)
        self.assertIn("not the dataset signing key", str(refused.exception))
        with open(self.meta) as fh:
            self.assertNotIn("signature", json.load(fh))

    def test_no_key_is_refused(self):
        with self.assertRaises(sm.SignError) as refused:
            sm.sign(self.meta, os.path.join(self.dir, "missing.pem"))
        self.assertIn("no signing key", str(refused.exception))

    def test_a_key_openssl_cannot_read_is_refused(self):
        bad = os.path.join(self.dir, "bad.pem")
        with open(bad, "w") as fh:
            fh.write("not a key")
        with self.assertRaises(sm.SignError):
            sm.sign(self.meta, bad)

    def test_the_key_path_defaults_and_is_overridable(self):
        env = {k: v for k, v in os.environ.items() if k != "DEN_DATASET_SIGNING_KEY"}
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(sm.key_path(), os.path.expanduser("~/.config/den/dataset-signing.pem"))
        with unittest.mock.patch.dict(os.environ, {"DEN_DATASET_SIGNING_KEY": self.key}):
            self.assertEqual(sm.key_path(), self.key)

    def test_no_openssl_with_ed25519_names_the_fix(self):
        with unittest.mock.patch.object(sm, "_candidates", lambda: iter([["false"]])):
            with self.assertRaises(sm.SignError) as refused:
                sm.openssl()
        self.assertIn("LibreSSL", str(refused.exception))
        self.assertIn("openssl@3", str(refused.exception))


if __name__ == "__main__":
    unittest.main()
