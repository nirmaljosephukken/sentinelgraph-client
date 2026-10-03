"""Synchronous wrapper around an ADK runner for the Streamlit console."""
from __future__ import annotations

import asyncio
import uuid

from google.adk.runners import InMemoryRunner
from google.genai import types

from sentinel_adk.agent import root_agent

APP = "sentinelgraph"


class Chat:
    def __init__(self, user_id: str = "analyst"):
        self.runner = InMemoryRunner(agent=root_agent, app_name=APP)
        self.user_id = user_id
        self.session_id = uuid.uuid4().hex
        asyncio.run(self.runner.session_service.create_session(app_name=APP, user_id=user_id,
                                                               session_id=self.session_id))

    def ask(self, text: str) -> tuple[str, list[dict]]:
        """Returns the final answer and the steps (tool calls and results) the agent took."""
        msg = types.Content(role="user", parts=[types.Part(text=text)])
        steps, answer = [], ""
        for ev in self.runner.run(user_id=self.user_id, session_id=self.session_id, new_message=msg):
            if not ev.content or not ev.content.parts:
                continue
            for p in ev.content.parts:
                if p.function_call:
                    steps.append({"kind": "call", "name": p.function_call.name, "args": dict(p.function_call.args or {})})
                elif p.function_response:
                    steps.append({"kind": "result", "name": p.function_response.name,
                                  "response": p.function_response.response})
                elif p.text and ev.author == root_agent.name:
                    answer = p.text if ev.is_final_response() else answer
        return answer, steps
