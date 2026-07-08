"""Vertex Agent engine entrypoint (source package deploy)

Runtime loads ``rag_agent.agent_engine_entry:a2a_agent`` - an ``A2aAgent`` bound to ``RagAgentExecutor`` so the engine is
**A2A-compliant** and discoverable in Agent Registry.
"""

from __future__ import annotations

import os
from typing import Any

os.environ.setdefault("DEPLOYMENT_MODE", "vertex")
os.environ["ADK_DISABLE_JSON_SCHEMA_FOR_FUNC_DECL"] = "1"

from vertexai.preview.reasoning_engines import A2aAgent

from rag_agent.agent_card import rag_agent_card
from rag_agent.executor import RagAgentExecutor
from rag_agent.firestore_task_store import FirestoreTaskStore

_process_task_store: FirestoreTaskStore | None = None


def _singleton_firestore_task_store(**_kwargs: Any) -> FirestoreTaskStore:
    """Distributed persistent task store using Google Cloud Firestore."""
    global _process_task_store
    if _process_task_store is None:
        # Save tasks to rag_tasks collection in Firestore
        _process_task_store = FirestoreTaskStore(collection_name="rag_tasks")
    return _process_task_store


class PlaygroundCompatibleA2aAgent(A2aAgent):
    """Exposes standard A2A operations made compatible with the Google Cloud Console Playground."""

    def set_up(self):
        super().set_up()
        try:
            from rag_agent.telemetry import setup_telemetry
            setup_telemetry()
        except Exception as e:
            print(f"[telemetry] Failed to initialize telemetry: {e}")

    def register_operations(self) -> dict[str, list[str]]:
        routes = super().register_operations()
        # Map the clean root query interface for the Playground Console
        routes[""] = ["query"]
        return routes

    def _extract_query_text(self, val: Any) -> str:
        """Helper to extract a raw string query from various input formats."""
        if not val:
            return ""
        if isinstance(val, str):
            return val
        if isinstance(val, dict):
            # Check for {"parts": [...]}
            parts = val.get("parts")
            if parts:
                return self._extract_query_text(parts)
            # Check for {"text": "..."}
            if "text" in val:
                return self._extract_query_text(val["text"])
            # Check for {"content": ...}
            if "content" in val:
                return self._extract_query_text(val["content"])
            return str(val)
        if isinstance(val, list):
            extracted = []
            for item in val:
                txt = self._extract_query_text(item)
                if txt:
                    extracted.append(txt)
            return " ".join(extracted)
        return str(val)

    async def query(
        self, input: str = "", text: str = "", query: str = "", **kwargs: Any
    ) -> str:
        """Standard query interface for testing in the GCP Console Playground.

        CRITICAL: Returns a raw string directly so the front-end chat interface
        can seamlessly render the text bubble response.
        """
        raw_query = input or text or query or ""
        user_query = self._extract_query_text(raw_query)
        if not user_query:
            return "No query provided."

        executor_builder = self._tmpl_attrs.get("agent_executor_builder")
        if not executor_builder:
            return "Executor not configured."

        executor = executor_builder(**self._tmpl_attrs.get("agent_executor_kwargs"))
        executor.init_runner()

        # Real-time session context parameters extracted or defaulted for tracing stability
        user_id = "playground_user"
        session_id = "playground_session"

        from google.genai import types

        content = types.Content(role="user", parts=[types.Part(text=user_query)])

        user_email = kwargs.get("user_email", "guest@example.com")
        user_groups = kwargs.get("user_groups", [])

        from rag_agent.agent_rag import current_user_email, current_user_groups
        email_token = current_user_email.set(user_email)
        groups_token = current_user_groups.set(user_groups)

        try:
            session = await executor.runner.session_service.get_session(
                app_name=executor.runner.app_name,
                user_id=user_id,
                session_id=session_id,
            ) or await executor.runner.session_service.create_session(
                app_name=executor.runner.app_name,
                user_id=user_id,
                session_id=session_id,
            )

            final_event = None
            async for event in executor.runner.run_async(
                session_id=session.id,
                user_id=user_id,
                new_message=content,
            ):
                if event.is_final_response():
                    final_event = event

            if final_event and final_event.content and final_event.content.parts:
                response_text = "".join(
                    part.text
                    for part in final_event.content.parts
                    if hasattr(part, "text") and part.text
                )
                if response_text:
                    return response_text

            return "No response text generated."
        except Exception as e:
            return f"Error running query: {str(e)}"
        finally:
            current_user_email.reset(email_token)
            current_user_groups.reset(groups_token)


a2a_agent = PlaygroundCompatibleA2aAgent(
    agent_card=rag_agent_card,
    agent_executor_builder=RagAgentExecutor,
    task_store_builder=_singleton_firestore_task_store,
)
