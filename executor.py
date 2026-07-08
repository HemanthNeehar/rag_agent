"""AgentExecutor for the RAG agent (A2A on Agent Runtime)."""

import os
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState, TextPart, UnsupportedOperationError
from a2a.utils import new_agent_text_message
from a2a.utils.errors import ServerError
from google.adk import Runner
from google.adk.artifacts import InMemoryArtifactService
from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
from google.adk.sessions import InMemorySessionService
from google.genai import types


class RagAgentExecutor(AgentExecutor):
    """Runs the ADK RAG agent in response to A2A ``Task`` requests."""

    def __init__(self) -> None:
        self.agent = None
        self.runner: Runner | None = None

    def init_runner(self) -> None:
        if self.agent is None:
            if os.getenv("RAG_CORPUS_ID"):
                from rag_agent.agent_rag import root_agent
                self.agent = root_agent
            else:
                from rag_agent.agent import root_agent
                self.agent = root_agent

        if self.runner is None:
            self.runner = Runner(
                app_name=self.agent.name,
                agent=self.agent,
                artifact_service=InMemoryArtifactService(),
                session_service=InMemorySessionService(),
                memory_service=InMemoryMemoryService(),
            )

    async def cancel(self, context: RequestContext, event_queue: EventQueue):
        raise ServerError(error=UnsupportedOperationError())

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        self.init_runner()

        if not context.message:
            return

        user_id = (
            context.message.metadata.get("user_id")
            if context.message and context.message.metadata
            else "a2a_user"
        )

        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        if not context.current_task:
            await updater.submit()

        await updater.start_work()

        query = context.get_user_input()
        content = types.Content(role="user", parts=[types.Part(text=query)])

        metadata = context.message.metadata or {} if context.message else {}
        user_email = metadata.get("user_email", "guest@example.com")
        user_groups = metadata.get("user_groups", [])

        from rag_agent.agent_rag import current_user_email, current_user_groups
        email_token = current_user_email.set(user_email)
        groups_token = current_user_groups.set(user_groups)

        try:
            session = await self.runner.session_service.get_session(
                app_name=self.runner.app_name,
                user_id=user_id,
                session_id=context.context_id,
            ) or await self.runner.session_service.create_session(
                app_name=self.runner.app_name,
                user_id=user_id,
                session_id=context.context_id,
            )

            final_event = None
            async for event in self.runner.run_async(
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
                    await event_queue.enqueue_event(
                        new_agent_text_message(
                            response_text,
                            context_id=context.context_id,
                            task_id=context.task_id,
                        )
                    )
                    await updater.add_artifact(
                        [TextPart(text=response_text)], name="result"
                    )
                    await updater.complete()
                    return

            await updater.update_status(
                TaskState.failed,
                message=new_agent_text_message(
                    "Failed to generate a final response with text content."
                ),
                final=True,
            )

        except Exception as e:
            await updater.update_status(
                TaskState.failed,
                message=new_agent_text_message(f"Execution failed: {str(e)}"),
                final=True,
            )
        finally:
            current_user_email.reset(email_token)
            current_user_groups.reset(groups_token)
