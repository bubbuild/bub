from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Queue
from threading import Thread

import pytest


def run_python(code: str, tmp_path: Path, **variables: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OTEL_", "LOGFIRE_", "BUB_"))}
    env.update(variables)
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def collector() -> Iterator[tuple[str, Queue[tuple[str, str | None, bytes]]]]:
    pytest.importorskip("opentelemetry.sdk.trace")
    requests: Queue[tuple[str, str | None, bytes]] = Queue()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            requests.put((
                self.path,
                self.headers.get("x-project-name"),
                self.rfile.read(int(self.headers["Content-Length"])),
            ))
            self.send_response(200)
            self.end_headers()

        def log_message(self, fmt: str, *args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", requests
        finally:
            server.shutdown()
            thread.join()


@pytest.mark.parametrize("generic_endpoint", [False, True])
def test_cli_exports_once_and_flushes_at_exit_without_configuring_logfire(
    tmp_path: Path, collector: tuple[str, Queue[tuple[str, str | None, bytes]]], generic_endpoint: bool
) -> None:
    pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    url, requests = collector
    endpoint = (
        {"OTEL_EXPORTER_OTLP_ENDPOINT": url}
        if generic_endpoint
        else {"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": f"{url}/v1/traces"}
    )
    run_python(
        """
try:
    import logfire
except ImportError:
    pass
else:
    # Pydantic may import Logfire's plugin, but Bub must not configure its exporter.
    def unexpected_configuration(*args, **kwargs):
        raise SystemExit('OTLP must not configure Logfire')
    logfire.configure = unexpected_configuration
from bub.__main__ import _instrument_bub
from bub.tracing import Span
_instrument_bub('INFO')  # CLI import already configured it; repeat must not duplicate exports.
root = Span('invoke_agent bub', {'gen_ai.operation.name': 'invoke_agent'})
with root.activate():
    child = Span('chat test', {'gen_ai.operation.name': 'chat'})
    child.end()
root.end()
# No explicit flush: the process must export queued spans on normal exit.
""",
        tmp_path,
        **endpoint,
        OTEL_SERVICE_NAME="bub-test",
        OTEL_EXPORTER_OTLP_HEADERS="x-project-name=test-project",
        OTEL_BSP_SCHEDULE_DELAY="600000",
    )
    path, project, body = requests.get(timeout=2)
    assert path == "/v1/traces"
    assert project == "test-project"
    request = ExportTraceServiceRequest.FromString(body)
    (resource,) = request.resource_spans
    assert any(a.key == "service.name" and a.value.string_value == "bub-test" for a in resource.resource.attributes)
    spans = [span for scope in resource.scope_spans for span in scope.spans]
    assert {span.name for span in spans} == {"invoke_agent bub", "chat test"}
    root = next(span for span in spans if span.name == "invoke_agent bub")
    child = next(span for span in spans if span.name == "chat test")
    assert child.parent_span_id == root.span_id
    assert child.trace_id == root.trace_id
    assert requests.empty()


@pytest.mark.parametrize("mode", ["no_endpoint", "disabled", "missing_sdk"])
def test_otlp_stays_noop_when_not_enabled_or_dependencies_are_missing(tmp_path: Path, mode: str) -> None:
    env = {} if mode == "no_endpoint" else {"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://127.0.0.1:1/v1/traces"}
    if mode == "disabled":
        env["OTEL_SDK_DISABLED"] = "true"
    blocker = (
        """
import sys
from importlib.abc import MetaPathFinder
class BlockSDK(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('opentelemetry.sdk', 'opentelemetry.exporter')):
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, BlockSDK())
"""
        if mode == "missing_sdk"
        else ""
    )
    run_python(
        blocker
        + """
from bub.tracing import Span, configure_otlp
configure_otlp()
span = Span('noop')
assert not span.recording
span.end()
""",
        tmp_path,
        **env,
    )


def test_otlp_preserves_application_provider(tmp_path: Path) -> None:
    pytest.importorskip("opentelemetry.sdk.trace")
    run_python(
        """
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from bub.tracing import Span, configure_otlp
provider = TracerProvider()
exporter = InMemorySpanExporter()
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)
configure_otlp()
assert trace.get_tracer_provider() is provider
Span('existing').end()
assert len(exporter.get_finished_spans()) == 1
""",
        tmp_path,
        OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="http://127.0.0.1:1/v1/traces",
    )


def test_otlp_rejects_unsupported_protocol(tmp_path: Path) -> None:
    pytest.importorskip("opentelemetry.sdk.trace")
    run_python(
        """
from bub.tracing import configure_otlp
try:
    configure_otlp()
except ValueError as exc:
    assert 'http/protobuf' in str(exc)
else:
    raise AssertionError('unsupported protocol silently accepted')
""",
        tmp_path,
        OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="http://127.0.0.1:1",
        OTEL_EXPORTER_OTLP_TRACES_PROTOCOL="grpc",
    )
