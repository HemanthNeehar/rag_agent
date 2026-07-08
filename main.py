"""RAG Agent entry : local A2A (uvicorn) or vertex ``A2aAgent`` (Agent Engine/deploy)."""

import os
from pathlib import Path
from dotenv import load_dotenv

os.environ["ADK_DISABLE_JSON_SCHEMA_FOR_FUNC_DECL"] = "1"
load_dotenv(Path(__file__).parent / ".env")

mode = os.getenv("DEPLOYMENT_MODE", "local")

_vertex_a2a_task_store_singleton: list = []

if mode == "local":
    import uvicorn
    from a2a.types import TransportProtocol
    from google.adk.a2a.utils.agent_to_a2a import to_a2a
    if os.getenv("RAG_CORPUS_ID"):
        from rag_agent.agent_rag import root_agent
    else:
        from rag_agent.agent import root_agent
    from rag_agent.agent_card import rag_agent_card

    port = int(os.getenv("RAG_AGENT_PORT", "8015"))
    print(f"[rag_agent] Starting local A2A server on port {port}...")

    _local_card = rag_agent_card.model_copy()
    _local_card.preferred_transport = TransportProtocol.jsonrpc

    a2a_app = to_a2a(root_agent, port=port, agent_card=_local_card)

    if __name__ == "__main__":
        uvicorn.run(a2a_app, host="0.0.0.0", port=port, log_level="info")

elif mode == "vertex":
    from typing import Any
    from vertexai.preview.reasoning_engines import A2aAgent

    from rag_agent.agent_card import rag_agent_card
    from rag_agent.executor import RagAgentExecutor
    from rag_agent.firestore_task_store import FirestoreTaskStore

    print(
        "[rag_agent] Building Vertex AI A2aAgent instance (for deploy.py)..."
    )

    def _singleton_task_store(**_kwargs: Any) -> FirestoreTaskStore:
        if not _vertex_a2a_task_store_singleton:
            _vertex_a2a_task_store_singleton.append(
                FirestoreTaskStore(collection_name="rag_tasks")
            )
        return _vertex_a2a_task_store_singleton[0]

    class PlaygroundCompatibleA2aAgent(A2aAgent):
        """Exposes standard A2A operations made compatible with the Google Cloud Console Playground."""

        def register_operations(self) -> dict[str, list[str]]:
            routes = super().register_operations()
            routes[""] = ["query"]
            return routes

        async def query(
            self, input: str = "", text: str = "", query: str = "", **kwargs: Any
        ) -> str:
            """Standard query interface for testing in the GCP Console Playground."""
            user_query = input or text or query or ""
            if not user_query:
                return "No query provided."

            executor_builder = self._tmpl_attrs.get("agent_executor_builder")
            if not executor_builder:
                return "Executor not configured."

            executor = executor_builder(**self._tmpl_attrs.get("agent_executor_kwargs"))
            executor.init_runner()

            user_id = "playground_user"
            session_id = "playground_session"

            from google.genai import types

            content = types.Content(role="user", parts=[types.Part(text=user_query)])

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

    a2a_agent = PlaygroundCompatibleA2aAgent(
        agent_card=rag_agent_card,
        agent_executor_builder=RagAgentExecutor,
        task_store_builder=_singleton_task_store,
    )

else:
    raise ValueError(f"Unknown deployment mode: {mode}. Must be 'local' or 'vertex'")
