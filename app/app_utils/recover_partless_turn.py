"""Recover from Gemini Enterprise "part-less" turns.

Gemini Enterprise can deliver a chat turn whose message has no content parts at
all (observed when a file upload's content is not forwarded). ADK's runner rejects
an empty message inside ``Runner._append_new_message_to_session``::

    if not new_message.parts:
        raise ValueError('No parts in the new_message.')

That check runs during invocation setup, *before* the root agent is invoked, so
the turn produces zero events and the agent never gets a chance to handle it.
Agent Engine surfaces it as::

    Reasoning Engine stream closed cleanly without producing any events
    ... FAILED_PRECONDITION ... attempt=3/3

This plugin runs on ``on_user_message_callback`` — the one hook that fires before
that validation — and substitutes a placeholder part when the incoming message has
none, so the turn runs normally and the agent can respond instead of crashing.

Register it on the App (or a Runner built from that App)::

    from google.adk.apps import App
    from recover_partless_turn import RecoverPartlessTurn

    app = App(name="my_agent", root_agent=root_agent, plugins=[RecoverPartlessTurn()])

Verified against google-adk==1.34.0. Depends only on google-adk and google-genai.
"""

from __future__ import annotations

from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types


class RecoverPartlessTurn(BasePlugin):
    """Guarantee the incoming user message has at least one content part.

    When Gemini Enterprise sends a turn with an empty ``parts`` list (or ``None``),
    this returns a copy carrying a single placeholder text part. ADK then accepts
    the message and the agent runs as usual. Any message that already has parts is
    left untouched (the callback returns ``None``).

    Args:
        placeholder: Text to insert when the message has no parts.
    """

    def __init__(self, placeholder: str = "(empty message)") -> None:
        super().__init__(name="recover_partless_turn")
        self._placeholder = placeholder

    async def on_user_message_callback(
        self,
        *,
        invocation_context: InvocationContext,
        user_message: types.Content,
    ) -> types.Content | None:
        if user_message is not None and not user_message.parts:
            return user_message.model_copy(
                update={"parts": [types.Part.from_text(text=self._placeholder)]}
            )
        return None  # message already has parts — proceed unchanged

    async def on_event_callback(
        self, *, invocation_context: InvocationContext, event: Event
    ) -> Event | None:
        if event is not None and not event.invocation_id:
            event.invocation_id = invocation_context.invocation_id
            return event
        return None
