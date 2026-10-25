"""Streaming a structured answer.

The research agent asks for a `GeneratedAnswer` and OpenAI fills a JSON schema,
so what streams back is JSON: `{"ans`, `wer":"`, `The minimum`. Showing an
answer as it is written therefore means growing one field out of a half-arrived
object, which is what `ProseAccumulator` does.

Two things are worth testing here, and they are different in kind:

* the accumulator, which is pure and gets the usual unit tests;
* that streaming did not change the *request*. The structured-output request is
  what the frozen eval baseline measures. An earlier attempt at token streaming
  changed the request shape — tools instead of a JSON schema — and cost
  `answer.refusal_correct` 1.000 → 0.902 (`docs/EXPERIMENTS.md`). That is not
  something to find in an eval run three weeks later, so it is asserted here
  against a stubbed transport: no network, no key.
"""

import json
from typing import Any

import httpx
import pytest
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessageChunk

from app.config import get_settings
from app.rag.generate import (
    Citation,
    GeneratedAnswer,
    ProseAccumulator,
    chunk_text,
    generate_answer,
    streaming_step,
)
from app.rag.retrieve import RetrievedChunk
from tests.conftest import FakeChatModel, fake_model

FEE_TEXT = "The minimum annual advisory fee is £1,500, regardless of portfolio value."


def chunk(content: str = FEE_TEXT) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=1, document_id="fee-schedule", page=1, content=content, distance=0.1
    )


def fragments(text: str, width: int) -> list[str]:
    return [text[i : i + width] for i in range(0, len(text), width)]


# --- The accumulator: pure, and where the prose actually comes from ----------


def test_the_answer_grows_as_the_json_arrives() -> None:
    accumulator = ProseAccumulator()
    body = GeneratedAnswer(
        answer="The minimum annual advisory fee is £1,500.",
        citations=[Citation(document_id="fee-schedule", page=1, quote="£1,500")],
    ).model_dump_json()

    deltas = [accumulator.feed(piece) for piece in fragments(body, 7)]

    emitted = [delta for delta in deltas if delta]
    assert len(emitted) > 1, "a reader should see the answer arrive in pieces"
    assert "".join(emitted) == "The minimum annual advisory fee is £1,500."


def test_nothing_is_emitted_before_the_answer_field_opens() -> None:
    """The opening chunks are the schema's own punctuation, not prose."""
    accumulator = ProseAccumulator()

    assert [accumulator.feed(piece) for piece in ('{"', "ans", 'wer":', ' "')] == ["", "", "", ""]


def test_a_delta_of_one_space_is_kept() -> None:
    """Whitespace is a word boundary. Dropping empty-looking deltas as a class
    would run the answer's words together."""
    accumulator = ProseAccumulator()
    accumulator.feed('{"answer": "The minimum')

    assert accumulator.feed(" ") == " "


def test_the_citations_that_follow_emit_no_prose() -> None:
    accumulator = ProseAccumulator()
    accumulator.feed('{"answer": "£1,500.", ')

    assert accumulator.feed('"citations": [{"document_id": "fee-schedule"') == ""


def test_a_closed_object_parses_into_the_answer_it_streamed() -> None:
    accumulator = ProseAccumulator()
    original = GeneratedAnswer(
        answer="A trade costs £9.95.",
        citations=[Citation(document_id="zt-fees", page=1, quote="£9.95")],
    )
    for piece in fragments(original.model_dump_json(), 5):
        accumulator.feed(piece)

    assert accumulator.final() == original


def test_a_truncated_stream_has_no_final_answer() -> None:
    """A provider that stops mid-object leaves prose on screen and no object.
    None here is what turns that into an error the caller reports."""
    accumulator = ProseAccumulator()
    accumulator.feed('{"answer": "The minimum annual')

    assert accumulator.prose == "The minimum annual"
    assert accumulator.final() is None


def test_a_reparse_that_shrinks_emits_nothing() -> None:
    """An escape sequence arriving half-written can shorten the parsed string.
    Emitting a delta from it would corrupt what is on screen; the turn's final
    `replace` is what makes the text exact."""
    accumulator = ProseAccumulator()
    accumulator.feed('{"answer": "fee is £1,500')
    before = accumulator.prose

    assert accumulator.feed("\\") == ""
    assert accumulator.prose == before


def test_content_blocks_are_read_as_text() -> None:
    """Some providers and langchain versions report content as blocks. Reading
    them means a provider change degrades to no streaming, not a crash."""
    blocks = [{"type": "text", "text": "fee is "}, {"type": "text", "text": "£1,500"}]

    assert chunk_text(blocks) == "fee is £1,500"
    assert chunk_text(None) == ""


# --- The chain's shape -------------------------------------------------------


def test_the_structured_chain_has_a_streamable_model_step() -> None:
    """`streaming_step` reaches past the parser for the model. If a langchain
    upgrade reshapes the chain this fails here, rather than silently falling
    back to a non-streaming answer nobody notices."""
    model = init_chat_model(get_settings().llm_model, temperature=0.0, api_key="test")

    step = streaming_step(model.with_structured_output(GeneratedAnswer))

    assert step is not None
    assert hasattr(step, "astream")


def test_a_fake_chat_model_has_no_streaming_step() -> None:
    """The unit tests' fake answers in one piece. `generate_answer` falls back
    to invoking it, which is why 700-odd tests did not have to change."""
    assert streaming_step(fake_model("£1,500.").with_structured_output(GeneratedAnswer)) is None


# --- The request, against a stubbed transport --------------------------------

_ANSWER = json.dumps({"answer": "£1,500.", "citations": []})
_COMPLETION = {
    "id": "chatcmpl-stub",
    "object": "chat.completion",
    "created": 0,
    "model": "stub",
    "choices": [
        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": _ANSWER}}
    ],
    "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
}
_CHUNKS = (
    {
        "id": "chatcmpl-stub",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "stub",
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": _ANSWER}}],
    },
    {
        "id": "chatcmpl-stub",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "stub",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    },
)
_SSE = "".join(f"data: {json.dumps(c)}\n\n" for c in _CHUNKS) + "data: [DONE]\n\n"

# `stream` differs by construction, and `stream_options` is how a provider is
# asked for usage on the last chunk. Everything else — the model, the messages,
# the temperature, and above all `response_format` — must be identical.
_STREAM_KEYS = ("stream", "stream_options")


def _stub_model(bodies: list[dict[str, Any]], *, streaming: bool) -> Any:
    """The configured model, with its transport stubbed and a dummy key.

    Built with the same `init_chat_model` call `app/llm.py` makes, from the
    same `LLM_MODEL`, so the request recorded here is the one production would
    send — the client and the key are the only substitutions.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if streaming:
            return httpx.Response(
                200, content=_SSE.encode(), headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(200, json=_COMPLETION)

    return init_chat_model(
        get_settings().llm_model,
        temperature=0.0,
        api_key="test",
        http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_streaming_sends_the_same_request_as_invoking() -> None:
    """The point of this whole file. Option (a) was chosen over generating
    prose and citations separately because it does not touch the request, so
    the eval baseline still describes what runs. This is that claim, in a test.
    """
    if not get_settings().llm_model.startswith("openai:"):
        pytest.skip("the stubbed responses are OpenAI's wire format")

    invoked: list[dict[str, Any]] = []
    streamed: list[dict[str, Any]] = []
    messages = [("system", "rules"), ("human", "what is the minimum fee?")]

    await (
        _stub_model(invoked, streaming=False)
        .with_structured_output(GeneratedAnswer)
        .ainvoke(messages)
    )
    streaming = _stub_model(streamed, streaming=True).with_structured_output(GeneratedAnswer)
    step = streaming_step(streaming)
    assert step is not None
    async for _ in step.astream(messages):
        pass

    (one,) = invoked
    (other,) = streamed
    assert {k: v for k, v in one.items() if k not in _STREAM_KEYS} == {
        k: v for k, v in other.items() if k not in _STREAM_KEYS
    }
    # Named explicitly, because this is the field the rejected experiment
    # changed: a forced tool call instead of a schema cost seven answers.
    assert one["response_format"]["type"] == "json_schema"


# --- generate_answer, streaming ----------------------------------------------


def streaming_model(answer: str, *citations: Citation) -> Any:
    return FakeChatModel(
        {GeneratedAnswer: GeneratedAnswer(answer=answer, citations=list(citations))},
        stream_schemas=(GeneratedAnswer,),
    )


async def test_the_answer_is_reported_as_it_is_written() -> None:
    deltas: list[str] = []

    result = await generate_answer(
        streaming_model("The minimum annual advisory fee is £1,500."),
        "what is the minimum fee?",
        [chunk()],
        on_prose=deltas.append,
    )

    assert len(deltas) > 1
    assert "".join(deltas) == "The minimum annual advisory fee is £1,500."
    assert result.answer == "The minimum annual advisory fee is £1,500."


async def test_the_streamed_prose_is_not_the_cleaned_answer() -> None:
    """Why the last `token` event replaces rather than appends: references are
    stripped from the finished answer, and the streamed text has them in."""
    deltas: list[str] = []

    result = await generate_answer(
        streaming_model("The fee is £1,500 (page 1)."),
        "what is the minimum fee?",
        [chunk()],
        on_prose=deltas.append,
    )

    assert "(page 1)" in "".join(deltas)
    assert result.answer == "The fee is £1,500."
    assert result.prose_references_written == 1


async def test_streaming_reports_the_tokens_it_spent() -> None:
    """Usage used to come from the raw message `include_raw=True` kept. On this
    path it rides the last chunk, and the eval's cost per query depends on it."""
    result = await generate_answer(
        streaming_model("£1,500."), "what is the minimum fee?", [chunk()]
    )

    assert result.usage.input_tokens == 11
    assert result.usage.output_tokens == 7


async def test_no_callback_still_answers() -> None:
    """Nothing listens during an eval run, and the streaming happens anyway:
    one code path, so the baseline measures what the graph runs."""
    result = await generate_answer(
        streaming_model("£1,500.", Citation(document_id="fee-schedule", page=1, quote="£1,500")),
        "what is the minimum fee?",
        [chunk()],
    )

    assert result.answer == "£1,500."
    assert result.citations[0].quote == "£1,500"


async def test_a_stream_that_never_completes_the_object_is_an_error() -> None:
    class Truncated:
        def with_structured_output(self, schema: type, **_: object) -> Any:
            class Chain:
                first = self

            return Chain()

        async def astream(self, messages: Any, **_: object) -> Any:
            yield AIMessageChunk(content='{"answer": "The minimum annual')

    with pytest.raises(ValueError, match="unparseable"):
        await generate_answer(Truncated(), "what is the minimum fee?", [chunk()])  # type: ignore[arg-type]
