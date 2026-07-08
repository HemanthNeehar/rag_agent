import os
import vertexai
from vertexai.preview import reasoning_engines
from rag_agent.agent_rag import root_agent

print("--- Inspecting AdkApp properties and methods ---")
adk_app = reasoning_engines.AdkApp(
    agent=root_agent,
    enable_tracing=False
)

print("Class:", type(adk_app))
print("Available attributes/methods:")
for attr in dir(adk_app):
    if not attr.startswith("_"):
        val = getattr(adk_app, attr)
        print(f"  {attr}: {type(val)}")
