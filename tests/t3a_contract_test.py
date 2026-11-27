"""Check the PR gate and the quality response schema at the locked web revision."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from data_policy_workflows_test import ROOT, step_script


W2A = "9132c5dc2e40f94d96151fbcc2989abbbb53d778"
QUALITY_GAPS = "/api/v1/quality/gaps"
WATERMARK_FIELDS = {"local_durable", "projected", "remote_verified", "source_latest"}
OBSERVATION_FIELDS = {"channel", "instrument_id", "epoch", "seq", "exchange_ts", "receive_ts"}


def resolve(document, schema):
    if "$ref" in schema:
        prefix = "#/components/schemas/"
        reference = schema["$ref"]
        if not reference.startswith(prefix):
            raise ValueError(f"expected a local schema reference, got {reference}")
        return document["components"]["schemas"][reference.removeprefix(prefix)]
    return schema


def response_schema(document, path):
    response = document["paths"][path]["get"]["responses"]["200"]
    return resolve(document, response["content"]["application/json"]["schema"])


def nullable_schema(document, schema):
    options = schema["oneOf"]
    non_null = [option for option in options if option.get("type") != "null"]
    if len(options) != 2 or len(non_null) != 1:
        raise ValueError("expected one null schema and one value schema")
    return resolve(document, non_null[0])


def quality_watermarks(document):
    page = response_schema(document, QUALITY_GAPS)
    coverage = resolve(document, page["properties"]["coverage"]["items"])
    return nullable_schema(document, coverage["properties"]["watermarks"])


class T3aContractTests(unittest.TestCase):
    def test_docs_pr_gate_rejects_refs_only(self):
        script = step_script("docs-check", "docs", "检查文档与 PR 描述")
        details = "Refs #44\n\n## 测试结果\n合成检查。"
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "python").symlink_to(sys.executable)
            env = {**os.environ, "PATH": f"{directory}:{os.environ['PATH']}",
                   "HEAD_REF": "issue-44-contracts-gitlinks-e2e"}
            for body, expected in ((details, 1), (f"Closes #53\n{details}", 0)):
                with self.subTest(expected=expected):
                    result = subprocess.run(
                        ["bash", "-e", "-o", "pipefail", "-c", script], cwd=ROOT,
                        env={**env, "PR_BODY": body}, capture_output=True, text=True, timeout=15,
                    )
                    self.assertEqual(result.returncode, expected, result.stderr)
                    if expected:
                        self.assertIn("PR 描述缺少 Closes #<号>", result.stderr)

    def assert_quality_response_shape(self, document):
        # web/tests/quality_publish.rs also exercises the router with a synthetic
        # F3 report, including an exact projected position above 2**53.
        schema = quality_watermarks(document)
        properties = schema["properties"]
        self.assertEqual(set(properties), WATERMARK_FIELDS, "quality watermarks response fields")
        self.assertEqual(set(schema["required"]), {"local_durable", "projected"})
        for field in ("local_durable", "projected"):
            self.assertEqual(resolve(document, properties[field])["type"], "string")
        self.assertEqual(nullable_schema(document, properties["remote_verified"])["type"], "string")
        source = nullable_schema(document, properties["source_latest"])
        self.assertEqual(set(source["properties"]), OBSERVATION_FIELDS)
        self.assertEqual(set(source["required"]), OBSERVATION_FIELDS - {"exchange_ts"})
        for field in ("epoch", "seq", "receive_ts"):
            self.assertEqual(resolve(document, source["properties"][field])["type"], "string")
        self.assertEqual(nullable_schema(document, source["properties"]["exchange_ts"])["type"], "string")

    def test_quality_schema_collision_is_limited_to_the_w2a_pin(self):
        document = json.loads((ROOT / "contracts/web.openapi.json").read_text())
        pin = subprocess.check_output(["git", "rev-parse", "HEAD:web"], cwd=ROOT, text=True).strip()
        if pin != W2A:
            self.assert_quality_response_shape(document)
            return

        baseline = json.loads((ROOT / "frontend/openapi/web.openapi.json").read_text())
        self.assert_quality_response_shape(baseline)
        with self.assertRaisesRegex(AssertionError, "quality watermarks response fields"):
            self.assert_quality_response_shape(document)
        schema = quality_watermarks(document)
        self.assertEqual(schema, response_schema(document, "/api/v1/market/watermarks"))
        self.assertEqual(set(schema["required"]), {"access", "as_of", "watermarks"})
        upstream = resolve(document, schema["properties"]["watermarks"])
        source = nullable_schema(document, upstream["properties"]["source_latest"])
        self.assertEqual(set(source["required"]), OBSERVATION_FIELDS)
        print(f"W6 pending: web {pin} exports finance watermarks for {QUALITY_GAPS}", flush=True)


if __name__ == "__main__":
    unittest.main()
