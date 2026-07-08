"""Deploy the RAG agent to Vertex AI Agent Engine as **A2A** (``A2aAgent`` + ``AgentExecutor``).

Uses **Deploy from source files** (``source_packages`` + ``entrypoint_module`` + ``entrypoint_object`` + ``class_methods``),
matching Agent Runtime guidance so the engine id is **A2A-compliant** and appears correctly in Agent registry.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from google.protobuf import json_format

from dotenv import load_dotenv

_RESERVED_DEPLOYMENT_ENV_NAMES = frozenset(
    {
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_QUOTA_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "PORT",
        "K_SERVICE",
        "K_REVISION",
        "K_CONFIGURATION",
        "GOOGLE_APPLICATION_CREDENTIALS",
    }
)


def _ensure_project_a2a_dependencies() -> None:
    try:
        from a2a.types import TextPart
    except ImportError as e:
        print(
            "[deploy] ERROR: Cannot import A2A types with this interpreter. \n"
            f" sys.executable = {sys.executable}\n"
            f" Import error: {e}",
            file=sys.stderr,
        )
        sys.exit(1)


_ensure_project_a2a_dependencies()

_deploy_dir = Path(__file__).resolve().parent
_repo_root = _deploy_dir.parent
_env = _repo_root / ".env"
if _env.is_file():
    load_dotenv(_env)
load_dotenv(_deploy_dir / ".env", override=True)
os.environ["DEPLOYMENT_MODE"] = "vertex"
os.environ["ADK_DISABLE_JSON_SCHEMA_FOR_FUNC_DECL"] = "1"
# os.environ["GCE_METADATA_HOST"] = "127.0.0.1"

import vertexai

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip() or "gebu-demo-sandbox"
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1").strip()
CREDENTIALS_FILE = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()

SERVICE_ACCOUNT = os.getenv("AGENT_RUNTIME_SERVICE_ACCOUNT", "").strip()

if CREDENTIALS_FILE and os.path.isfile(CREDENTIALS_FILE):
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = CREDENTIALS_FILE

print(
    "[deploy] A2A deploy - RAG agent -> Vertex Agent Engine (source packages)"
)
print(f"[deploy] Project: {PROJECT_ID} | Location: {LOCATION}")

vertexai.init(project=PROJECT_ID, location=LOCATION)


def _plain(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or default).strip()


def _a2a_agent_card_json_for_deploy(agent: Any) -> str:
    """Serialize ``A2aAgent.agent_card`` for ``class_methods``/ Agent Registry.

    ``vertexai._genai._agent_engines_utils._generate_class_methods_spec_or_raise`` uses
    ``json_format.MessageToJson(agent.agent_card)``, which only accepts protobuf messages.
    ``A2aAgent`` stores the A2A SDK **Pydantic** ``AgentCard``, so we JSON-encode it here.
    """

    card = getattr(agent, "agent_card", None)
    if card is None:
        return "{}"

    model_dump = getattr(card, "model_dump", None)
    if callable(model_dump):
        return json.dumps(model_dump(mode="json"), default=str)

    dict_fn = getattr(card, "dict", None)
    if callable(dict_fn):
        return json.dumps(dict_fn())

    return json_format.MessageToJson(card)


def _class_methods_for_rag_a2a() -> list[dict[str, Any]]:
    """Mirror ``_generate_class_methods_spec_or_raise`` with Pydantic safe ``a2a_agent_card``."""

    from rag_agent.main import a2a_agent
    from vertexai._genai import _agent_engines_utils as u
    from vertexai._genai._agent_engines_utils import (
        _A2A_AGENT_CARD,
        _MODE_KEY_IN_SCHEMA,
    )

    agent = a2a_agent
    operations = u._get_registered_operations(agent=agent)

    if isinstance(agent, u.ModuleAgent):
        agent = agent.clone()  # type: ignore[assignment]

        try:
            agent.set_up()
        except Exception as e:
            raise ValueError(f"Failed to set up agent {agent}: {e}") from e

    _log = logging.getLogger("vertexai_genai.agentengines")
    class_methods_spec: list[Any] = []

    for mode, method_names in operations.items():
        for method_name in method_names:
            if not hasattr(agent, method_name):
                raise ValueError(
                    f"Method `{method_name}` defined in `register_operations` not found on agent."
                )

            method = getattr(agent, method_name)
            try:
                schema_dict = u._generate_schema(method, schema_name=method_name)
            except Exception as e:
                _log.warning("failed to generate schema for %s: %s", method_name, e)
                continue

            class_method = u._to_proto(schema_dict)
            class_method[_MODE_KEY_IN_SCHEMA] = mode
            if hasattr(agent, "agent_card"):
                class_method[_A2A_AGENT_CARD] = _a2a_agent_card_json_for_deploy(agent)
            class_methods_spec.append(class_method)

    return [u._to_dict(s) for s in class_methods_spec]


_requirements_file_rel = "rag_agent/requirements.txt"
_requirements_path = (_deploy_dir / "requirements.txt").resolve()
if not _requirements_path.is_file():
    print(f"[deploy] ERROR: Missing {_requirements_path}", file=sys.stderr)
    sys.exit(1)

_python_version = _plain("AGENT_ENGINE_PYTHON_VERSION", "")

plain_env_vars: dict[str, str] = {
    "DEPLOYMENT_MODE": "vertex",
    "GOOGLE_GENAI_USE_VERTEXAI": _plain("GOOGLE_GENAI_USE_VERTEXAI", "TRUE") or "TRUE",
    "GEMINI_MODEL": _plain("GEMINI_MODEL", "gemini-2.5-flash") or "gemini-2.5-flash",
    "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY": "true",
    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "true",
    "ADK_DISABLE_JSON_SCHEMA_FOR_FUNC_DECL": "1",
}

for optional_key in (
    "RAG_AGENT_URL",
    "FIRESTORE_DATABASE_ID",
    "RAG_DATA_STORE_ID",
    "RAG_CORPUS_ID",
    "RAG_GCS_BUCKET_NAME",
    "RAG_DATA_STORE_LOCATION",
):
    v = _plain(optional_key)
    if v:
        plain_env_vars[optional_key] = v

_sanitized: dict[str, str] = {}
for key, val in plain_env_vars.items():
    if not val:
        continue
    if key in _RESERVED_DEPLOYMENT_ENV_NAMES:
        print(
            f"[deploy] WARNING: omitting reserved env var {key!r} (Agent Runtime).",
            file=sys.stderr,
        )
        continue

    _sanitized[key] = val
plain_env_vars = _sanitized

_original_cwd = os.getcwd()
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

try:
    os.chdir(_repo_root)
except OSError as e:
    print(f"[deploy] ERROR: Cannot chdir to {_repo_root}: {e}", file=sys.stderr)
    sys.exit(1)

print(f"[deploy] cwd for source_packages: {os.getcwd()}")
print("[deploy] source_packages: ['rag_agent']")
print("[deploy] entrypoint: rag_agent.agent_engine_entry:a2a_agent")
print(f"[deploy] requirements_file: {_requirements_file_rel}")

config: dict[str, Any] = {
    "source_packages": ["rag_agent"],
    "entrypoint_module": "rag_agent.agent_engine_entry",
    "entrypoint_object": "a2a_agent",
    "requirements_file": _requirements_file_rel,
    "class_methods": _class_methods_for_rag_a2a(),
    "display_name": "Corporate RAG Agent A2A",
}

config["agent_framework"] = "google-adk"
if _python_version:
    config["python_version"] = _python_version
if plain_env_vars:
    config["env_vars"] = plain_env_vars
if SERVICE_ACCOUNT:
    config["service_account"] = SERVICE_ACCOUNT
    print(f"[deploy] service_account: {SERVICE_ACCOUNT}")
else:
    print("[deploy] Using default Reasoning Engine service agent")

if plain_env_vars:
    print("[deploy] Runtime env_vars keys: " + ", ".join(sorted(plain_env_vars.keys())))
else:
    print("[deploy] Runtime env_vars keys: (none)")

try:
    client = vertexai.Client(project=PROJECT_ID, location=LOCATION)
    remote_agent = client.agent_engines.create(config=config)

    resource_name = (
        getattr(remote_agent, "resource_name", None)
        or getattr(getattr(remote_agent, "api_resource", None), "name", None)
        or ""
    )

    print("\n" + "=" * 60)
    print("CORPORATE RAG AGENT DEPLOYED (A2A/Agent Registry)")
    print("=" * 60)
    print(f"Resource name: {resource_name}")
    print("\nTest with:")
    print(f" export RESOURCE_NAME={resource_name}")
    print(' export GOOGLE_CLOUD_LOCATION={LOCATION}"')
    print(' export GOOGLE_CLOUD_PROJECT={PROJECT_ID}"')
    print("=" * 60)
    print(f"DEPLOY_RESOURCE_NAME={resource_name}")

except Exception as e:
    print(f"[deploy] ERROR during remote agent creation - {e}", file=sys.stderr)
    sys.exit(1)

finally:
    try:
        os.chdir(_original_cwd)
    except OSError:
        pass
