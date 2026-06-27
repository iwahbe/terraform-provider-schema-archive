import gzip
import random
import unittest

from dump import MAX_SCHEMA_GZ_BYTES, finalize_schema, tofu_started


class FinalizeSchemaTest(unittest.TestCase):
    def test_under_limit_compresses_and_roundtrips(self):
        schema = b'{"provider_schemas": {}}\n' * 1000
        status, schema_gz = finalize_schema(schema)
        self.assertEqual("done", status)
        self.assertIsNotNone(schema_gz)
        self.assertEqual(schema, gzip.decompress(schema_gz))

    def test_compression_is_deterministic(self):
        schema = b'{"a": 1}\n' * 5000
        self.assertEqual(finalize_schema(schema), finalize_schema(schema))

    def test_over_limit_is_rejected_and_not_written(self):
        incompressible = random.Random(0).randbytes(4096)
        status, schema_gz = finalize_schema(incompressible, limit=100)
        self.assertEqual("rejected", status)
        self.assertIsNone(schema_gz)

    def test_default_limit_is_eighty_mib(self):
        self.assertEqual(80 * 1024 * 1024, MAX_SCHEMA_GZ_BYTES)


class TofuStartedTest(unittest.TestCase):
    # The harness-permission bug produced an empty stdout and this stderr.
    HARNESS_STDERR = "Failed to determine current working directory: stat .: permission denied"
    # A genuine provider failure: tofu ran and reached provider resolution before failing.
    GENUINE_STDOUT = (
        "\nInitializing the backend...\n\nInitializing provider plugins...\n"
        '- Finding sunch4se/unifi versions matching "0.42.1"...\n'
    )
    GENUINE_STDERR = "\nError: Failed to install provider\n\n404 Not Found returned from github.com\n"
    # A genuine config failure that errors before provider resolution but after tofu starts.
    BAD_NAMESPACE_STDOUT = "\nInitializing the backend...\n"
    BAD_NAMESPACE_STDERR = (
        "\nError: Invalid provider namespace\n\n"
        'Invalid provider namespace "" in source "registry.opentofu.org/mattias-/buildkite"\n'
    )

    def test_harness_failure_did_not_start_tofu(self):
        self.assertFalse(tofu_started("", self.HARNESS_STDERR))

    def test_genuine_install_failure_started_tofu(self):
        self.assertTrue(tofu_started(self.GENUINE_STDOUT, self.GENUINE_STDERR))

    def test_invalid_namespace_failure_started_tofu(self):
        self.assertTrue(tofu_started(self.BAD_NAMESPACE_STDOUT, self.BAD_NAMESPACE_STDERR))


if __name__ == "__main__":
    unittest.main()
