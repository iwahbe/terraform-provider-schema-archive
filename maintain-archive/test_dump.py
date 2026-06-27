import gzip
import random
import unittest

from dump import MAX_SCHEMA_GZ_BYTES, finalize_schema


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


if __name__ == "__main__":
    unittest.main()
