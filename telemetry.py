import os
import logging
from typing import Optional

def setup_telemetry() -> None:
    """Sets up OpenTelemetry tracing for the A2A agent if GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY is enabled."""
    enable_telemetry = os.getenv("GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY", "false").lower() == "true"
    if not enable_telemetry:
        print("[telemetry] Telemetry environment variable not enabled. Proceeding with tracing disabled.")
        return

    project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
    location = os.getenv("GOOGLE_CLOUD_LOCATION") or os.getenv("GOOGLE_CLOUD_AGENT_ENGINE_LOCATION") or "us-central1"
    agent_engine_id = os.getenv("GOOGLE_CLOUD_AGENT_ENGINE_ID")

    if not project_id:
        print("[telemetry] GOOGLE_CLOUD_PROJECT is not set, tracing disabled.")
        return

    try:
        import opentelemetry
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        import google.auth
        import google.auth.transport.requests
        from google.cloud.aiplatform import version as aip_version
        import uuid
    except ImportError as e:
        print(f"[telemetry] Failed to import OpenTelemetry SDK libraries: {e}. Tracing disabled.")
        return

    print(f"[telemetry] Initializing OpenTelemetry tracing for project: {project_id}...")

    # Detect Cloud Resource ID
    cloud_resource_id = None
    if location and agent_engine_id:
        cloud_resource_id = f"//aiplatform.googleapis.com/projects/{project_id}/locations/{location}/reasoningEngines/{agent_engine_id}"

    # Build Resource
    resource_attrs = {
        "gcp.project_id": project_id,
        "cloud.account.id": project_id,
        "cloud.provider": "gcp",
        "cloud.platform": "gcp.agent_engine",
        "service.name": agent_engine_id or "rag-agent",
        "service.instance.id": f"{uuid.uuid4().hex}-{os.getpid()}",
        "cloud.region": location,
    }
    if cloud_resource_id:
        resource_attrs["cloud.resource_id"] = cloud_resource_id

    resource = Resource.create(attributes=resource_attrs)

    # Set up Tracer Provider if not already set or if it is proxy/noop provider
    try:
        tracer_provider = trace.get_tracer_provider()
    except Exception:
        tracer_provider = None

    from vertexai.agent_engines import _utils
    if not tracer_provider or _utils.is_noop_or_proxy_tracer_provider(tracer_provider):
        tracer_provider = TracerProvider(resource=resource)
        trace.set_tracer_provider(tracer_provider)

    # Configure OTLP HTTP trace exporter pointing to GCP Telemetry API
    try:
        credentials, _ = google.auth.default()
        authed_session = google.auth.transport.requests.AuthorizedSession(credentials=credentials)
        
        user_agent = f"Vertex-Agent-Engine/{aip_version.__version__} OTel-OTLP-Exporter-Python"
        
        span_exporter = OTLPSpanExporter(
            session=authed_session,
            endpoint="https://telemetry.googleapis.com/v1/traces",
            headers={"User-Agent": user_agent},
        )
        span_processor = BatchSpanProcessor(span_exporter=span_exporter)
        tracer_provider.add_span_processor(span_processor)
        print("[telemetry] OTLPSpanExporter successfully registered to telemetry.googleapis.com.")
    except Exception as e:
        # Code Safeguard: Gracefully log Trace Exporter failures to prevent 403 authorization
        # errors from crashing the agent thread.
        print(f"[telemetry] Warning: Failed to configure OTLPSpanExporter (usually permissions): {e}")

    # Instrument Google GenAI SDK
    try:
        from opentelemetry.instrumentation import google_genai
        google_genai.GoogleGenAiSdkInstrumentor().instrument()
        print("[telemetry] Instrumented google-genai SDK.")
    except Exception as e:
        print(f"[telemetry] Failed to instrument google-genai: {e}")

    # Instrument HTTPX (used for A2A calls)
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        HTTPXClientInstrumentor().instrument()
        print("[telemetry] Instrumented httpx client.")
    except Exception as e:
        print(f"[telemetry] Failed to instrument httpx: {e}")

    # Instrument GRPC
    try:
        from opentelemetry.instrumentation.grpc import GrpcInstrumentorClient
        GrpcInstrumentorClient().instrument()
        print("[telemetry] Instrumented grpc client.")
    except Exception as e:
        print(f"[telemetry] Failed to instrument grpc: {e}")
