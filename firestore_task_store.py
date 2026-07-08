import os
from google.cloud import firestore
from a2a.server.context import ServerCallContext
from a2a.server.tasks.task_store import TaskStore
from a2a.types import Task


class FirestoreTaskStore(TaskStore):
    """Distributed, persistent Firestore implementation of A2A TaskStore."""

    def __init__(self, collection_name: str = "a2a_tasks") -> None:
        project = os.getenv("GOOGLE_CLOUD_PROJECT") or "gebu-demo-sandbox"
        database = (
            os.getenv("FIRESTORE_DATABASE_ID")
            or "ai-studio-c65c8c94-4557-4ed7-87bc-594554053988"
        )
        self.db = firestore.AsyncClient(project=project, database=database)
        self.collection = self.db.collection(collection_name)

    async def save(self, task: Task, context: ServerCallContext | None = None) -> None:
        task_data = task.model_dump(mode="json")
        doc_ref = self.collection.document(task.id)
        await doc_ref.set(task_data)

    async def get(
        self, task_id: str, context: ServerCallContext | None = None
    ) -> Task | None:
        doc_ref = self.collection.document(task_id)
        doc = await doc_ref.get()
        if doc.exists:
            return Task.model_validate(doc.to_dict())
        return None

    async def delete(
        self, task_id: str, context: ServerCallContext | None = None
    ) -> None:
        doc_ref = self.collection.document(task_id)
        await doc_ref.delete()
