"""scripts/gateway-matrix.sh must FAIL against a leaking gateway and PASS against an isolating one.

A tiny stub stands in for the gateway. Tokens map to a context; objects live in a context.
``leaky=True`` ignores the context on reads (what a broken gateway would do), ``revoke_leaks``
keeps a revoked token alive. The script only talks HTTP, so this exercises it end to end.
"""

import json
import os
import shutil
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gateway-matrix.sh"
ARTIFACTS = "/api/2.0/mlflow-artifacts/artifacts/"

TOKENS = {
    "iop_token_of_a_" + "a" * 20: ("A", {"read", "write"}),
    "iop_token_of_a_read_" + "r" * 20: ("A", {"read"}),
    "iop_token_of_b_" + "b" * 20: ("B", {"read", "write"}),
    "iop_token_to_revoke_" + "v" * 20: ("A", {"read", "write"}),
}
TOKEN_A, TOKEN_A_READ, TOKEN_B, TOKEN_REVOKE = list(TOKENS)


class StubGateway:
    def __init__(self, *, leaky: bool = False, revoke_leaks: bool = False) -> None:
        self.leaky = leaky
        self.revoke_leaks = revoke_leaks
        self.revoked: set[str] = set()
        self.experiments: dict[str, tuple[str, str]] = {}  # id -> (context, name)
        self.runs: dict[str, str] = {}  # run id -> context
        self.models: dict[str, str] = {}  # name -> context
        self.blobs: dict[str, tuple[str, bytes]] = {}  # path -> (context, bytes)
        self.counter = 0
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, payload=b"", ctype="application/json"):
                if not isinstance(payload, bytes):
                    payload = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _who(self):
                header = self.headers.get("Authorization", "")
                token = header.removeprefix("Bearer ")
                if token not in TOKENS or (token in stub.revoked and not stub.revoke_leaks):
                    return None
                return TOKENS[token]

            def _handle(self, method):
                url = urlsplit(self.path)
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if url.path == "/_stub/revoke":
                    stub.revoked.add(TOKEN_REVOKE)
                    return self._send(200, {})
                who = self._who()
                if who is None:
                    return self._send(401, {"error_code": "UNAUTHENTICATED"})
                context, scopes = who
                body = json.loads(raw) if raw and not url.path.startswith(ARTIFACTS) else {}
                if url.path.startswith("/api/3.0/mlflow/workspaces") or "/users/" in url.path:
                    return self._send(403, {"error_code": "FORBIDDEN"})
                write = method != "GET" and "search" not in url.path
                if write and "write" not in scopes:
                    return self._send(403, {"error_code": "FORBIDDEN"})
                return self._route(method, url.path, query, body, raw, context)

            def _visible(self, owner, context):
                return stub.leaky or owner == context

            def _route(self, method, path, query, body, raw, context):
                if path.startswith(ARTIFACTS):
                    key = path.removeprefix(ARTIFACTS)
                    if method == "PUT":
                        if key in stub.blobs and not self._visible(stub.blobs[key][0], context):
                            return self._send(403, {})
                        stub.blobs[key] = (context, raw)
                        return self._send(200, {})
                    owner_blob = stub.blobs.get(key)
                    if owner_blob is None or not self._visible(owner_blob[0], context):
                        return self._send(404, {})
                    if method == "DELETE":
                        del stub.blobs[key]
                        return self._send(200, {})
                    return self._send(200, owner_blob[1], "application/octet-stream")
                if path.endswith("/experiments/create"):
                    stub.counter += 1
                    exp_id = str(1000 + stub.counter)
                    stub.experiments[exp_id] = (context, body["name"])
                    return self._send(200, {"experiment_id": exp_id})
                if path.endswith("/experiments/get"):
                    found = stub.experiments.get(query.get("experiment_id"))
                    if found is None or not self._visible(found[0], context):
                        return self._send(404, {"error_code": "RESOURCE_DOES_NOT_EXIST"})
                    return self._send(200, {"experiment": {"name": found[1]}})
                if path.endswith("/experiments/search"):
                    names = [n for c, n in stub.experiments.values() if self._visible(c, context)]
                    return self._send(200, {"experiments": [{"name": n} for n in names]})
                if path.endswith("/experiments/delete"):
                    stub.experiments.pop(body["experiment_id"], None)
                    return self._send(200, {})
                if path.endswith("/runs/create"):
                    stub.counter += 1
                    run_id = f"{stub.counter:032x}"
                    stub.runs[run_id] = context
                    uri = f"mlflow-artifacts:/workspaces/{context}/{body['experiment_id']}/{run_id}/artifacts"
                    return self._send(
                        200, {"run": {"info": {"run_id": run_id, "artifact_uri": uri}}}
                    )
                if path.endswith("/runs/get"):
                    owner = stub.runs.get(query.get("run_id"))
                    if owner is None or not self._visible(owner, context):
                        return self._send(404, {"error_code": "RESOURCE_DOES_NOT_EXIST"})
                    return self._send(200, {"run": {"info": {"run_id": query["run_id"]}}})
                if path.endswith("/runs/delete"):
                    owner = stub.runs.get(body["run_id"])
                    if owner is None or not self._visible(owner, context):
                        return self._send(404, {})
                    del stub.runs[body["run_id"]]
                    return self._send(200, {})
                if path.endswith("/registered-models/create"):
                    stub.models[body["name"]] = context
                    return self._send(200, {})
                if path.endswith("/registered-models/get"):
                    owner = stub.models.get(query.get("name"))
                    if owner is None or not self._visible(owner, context):
                        return self._send(404, {"error_code": "RESOURCE_DOES_NOT_EXIST"})
                    return self._send(200, {"registered_model": {"name": query["name"]}})
                if path.endswith("/registered-models/delete"):
                    stub.models.pop(body["name"], None)
                    return self._send(200, {})
                if path == "/":
                    return self._send(401, {})
                return self._send(404, {})

            def do_GET(self):
                self._handle("GET")

            do_POST = do_PUT = do_DELETE = lambda self: self._handle(self.command)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def left_behind(self):
        return len(self.experiments) + len(self.runs) + len(self.models) + len(self.blobs)


def run_matrix(stub, **extra):
    env = {
        "PATH": os.environ["PATH"],
        "GATEWAY_URL": stub.url,
        "TOKEN_A": TOKEN_A,
        "TOKEN_B": TOKEN_B,
        "TOKEN_A_READ": TOKEN_A_READ,
        "TOKEN_REVOKE": TOKEN_REVOKE,
        "REVOKE_COMMAND": f"curl -s -X POST {stub.url}/_stub/revoke",
        "REVOKE_WAIT_SECONDS": "3",
        **extra,
    }
    return subprocess.run(
        ["sh", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=120, check=False
    )


@unittest.skipUnless(shutil.which("curl") and shutil.which("python3"), "curl and python3 needed")
class GatewayMatrixTests(unittest.TestCase):
    def test_isolating_gateway_passes_and_cleans_up_everything(self):
        with StubGateway() as stub:
            result = run_matrix(stub)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("FAIL", result.stdout)
            for line in (
                "A creates an experiment",
                "A reads its artifact",
                "B cannot read A's artifact",
                "B: nonexistent run id",
                "revoked token refused",
                "cleanup: registered model",
            ):
                self.assertIn(line, result.stdout)
            self.assertEqual(stub.left_behind(), 0, "the matrix left objects behind")

    def test_leaking_gateway_fails_and_names_the_leaks(self):
        with StubGateway(leaky=True) as stub:
            result = run_matrix(stub)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            for leak in (
                "B cannot read A's experiment",
                "B cannot read A's run",
                "B cannot read A's artifact",
                "B cannot read A's registered model",
            ):
                self.assertIn(f"FAIL 200 (want 403 404) {leak}", result.stdout)
            self.assertEqual(stub.left_behind(), 0, "cleanup must also run when checks fail")

    def test_a_revocation_that_does_not_take_effect_fails(self):
        with StubGateway(revoke_leaks=True) as stub:
            result = run_matrix(stub)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("revoked token still accepted", result.stdout)

    def test_revocation_is_optional(self):
        with StubGateway() as stub:
            result = run_matrix(stub, TOKEN_REVOKE="", REVOKE_COMMAND="")
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("skip revocation", result.stdout)

    def test_secrets_never_reach_argv(self):
        # curl receives its config on stdin: the script must not put a token on a command line.
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("curl --config -", text)


if __name__ == "__main__":
    unittest.main()
