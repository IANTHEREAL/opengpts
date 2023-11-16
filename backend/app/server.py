import asyncio
import json
from pathlib import Path
from typing import Annotated, AsyncIterator, Optional, Sequence
from fastapi.exceptions import RequestValidationError

import orjson
from fastapi import BackgroundTasks, Cookie, FastAPI, Form, Request, UploadFile
from fastapi.staticfiles import StaticFiles
from gizmo_agent import agent, ingest_runnable
from langserve.callbacks import AsyncEventAggregatorCallback
from langchain.pydantic_v1 import ValidationError
from langchain.schema.messages import AnyMessage
from langchain.schema.runnable import RunnableConfig
from langserve import add_routes
from langserve.server import _get_base_run_id_as_str, _unpack_input
from langserve.serialization import WellKnownLCSerializer
from pydantic import BaseModel
from sse_starlette import EventSourceResponse
from typing_extensions import TypedDict

from app.storage import (
    get_assistant,
    get_thread_messages,
    list_assistants,
    list_public_assistants,
    list_threads,
    put_assistant,
    put_thread,
)
from app.stream import StreamMessagesHandler

app = FastAPI()

FEATURED_PUBLIC_ASSISTANTS = [
    "ba721964-b7e4-474c-b817-fb089d94dc5f",
    "dc3ec482-aafc-4d90-8a1a-afb9b2876cde",
]

# Get root of app, used to point to directory containing static files
ROOT = Path(__file__).parent.parent


def attach_user_id_to_config(
    config: RunnableConfig,
    request: Request,
) -> RunnableConfig:
    config["configurable"]["user_id"] = request.cookies["opengpts_user_id"]
    return config


add_routes(
    app,
    agent,
    config_keys=["configurable"],
    per_req_config_modifier=attach_user_id_to_config,
    enable_feedback_endpoint=True,
)

serializer = WellKnownLCSerializer()


class AgentInput(BaseModel):
    messages: Sequence[AnyMessage]


class CreateRunPayload(BaseModel):
    assistant_id: str
    thread_id: str
    stream: bool
    # TODO make optional
    input: AgentInput


@app.post("/runs")
async def create_run_endpoint(
    request: Request,
    opengpts_user_id: Annotated[str, Cookie()],
    background_tasks: BackgroundTasks,
):
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise RequestValidationError(errors=["Invalid JSON body"])
    assistant, state = await asyncio.gather(
        asyncio.get_running_loop().run_in_executor(
            None, get_assistant, opengpts_user_id, body["assistant_id"]
        ),
        asyncio.get_running_loop().run_in_executor(
            None, get_thread_messages, opengpts_user_id, body["thread_id"]
        ),
    )
    config: RunnableConfig = attach_user_id_to_config(
        {
            **assistant["config"],
            "configurable": {
                **assistant["config"]["configurable"],
                "thread_id": body["thread_id"],
                "assistant_id": body["assistant_id"],
            },
        },
        request,
    )
    try:
        input_ = _unpack_input(agent.get_input_schema(config).validate(body["input"]))
    except ValidationError as e:
        raise RequestValidationError(e.errors(), body=body)
    if body["stream"]:
        streamer = StreamMessagesHandler(state["messages"] + input_["messages"])
        event_aggregator = AsyncEventAggregatorCallback()
        config["callbacks"] = [streamer, event_aggregator]

        # Call the runnable in streaming mode,
        # add each chunk to the output stream
        async def consume_astream() -> None:
            try:
                async for chunk in agent.astream(input_, config):
                    await streamer.send_stream.send(chunk)
            except Exception as e:
                await streamer.send_stream.send(e)
            finally:
                await streamer.send_stream.aclose()

        # Start the runnable in the background
        task = asyncio.create_task(consume_astream())

        # Consume the stream into an EventSourceResponse
        async def _stream() -> AsyncIterator[dict]:
            has_sent_metadata = False

            async for chunk in streamer.receive_stream:
                if isinstance(chunk, BaseException):
                    yield {
                        "event": "error",
                        # Do not expose the error message to the client since
                        # the message may contain sensitive information.
                        # We'll add client side errors for validation as well.
                        "data": orjson.dumps(
                            {"status_code": 500, "message": "Internal Server Error"}
                        ).decode(),
                    }
                    raise chunk
                else:
                    if not has_sent_metadata and event_aggregator.callback_events:
                        yield {
                            "event": "metadata",
                            "data": orjson.dumps(
                                {"run_id": _get_base_run_id_as_str(event_aggregator)}
                            ).decode(),
                        }
                        has_sent_metadata = True

                    yield {
                        # EventSourceResponse expects a string for data
                        # so after serializing into bytes, we decode into utf-8
                        # to get a string.
                        "data": serializer.dumps(chunk).decode("utf-8"),
                        "event": "data",
                    }

            # Send an end event to signal the end of the stream
            yield {"event": "end"}
            # Wait for the runnable to finish
            await task

        return EventSourceResponse(_stream())
    else:
        background_tasks.add_task(agent.ainvoke, input_, config)
        return {"status": "ok"}  # TODO add a run id


@app.post("/ingest")
def ingest_endpoint(files: list[UploadFile], config: str = Form(...)):
    config = orjson.loads(config)
    return ingest_runnable.batch([file.file for file in files], config)


@app.get("/assistants/")
def list_assistants_endpoint(opengpts_user_id: Annotated[str, Cookie()]):
    """List all assistants for the current user."""
    return list_assistants(opengpts_user_id)


@app.get("/assistants/public/")
def list_public_assistants_endpoint(shared_id: Optional[str] = None):
    return list_public_assistants(
        FEATURED_PUBLIC_ASSISTANTS + ([shared_id] if shared_id else [])
    )


class AssistantPayload(TypedDict):
    name: str
    config: dict
    public: bool


@app.put("/assistants/{aid}")
def put_assistant_endpoint(
    aid: str,
    payload: AssistantPayload,
    opengpts_user_id: Annotated[str, Cookie()],
):
    return put_assistant(
        opengpts_user_id,
        aid,
        name=payload["name"],
        config=payload["config"],
        public=payload["public"],
    )


@app.get("/threads/")
def list_threads_endpoint(opengpts_user_id: Annotated[str, Cookie()]):
    return list_threads(opengpts_user_id)


@app.get("/threads/{tid}/messages")
def get_thread_messages_endpoint(opengpts_user_id: Annotated[str, Cookie()], tid: str):
    return get_thread_messages(opengpts_user_id, tid)


class ThreadPayload(TypedDict):
    name: str
    assistant_id: str


@app.put("/threads/{tid}")
def put_thread_endpoint(
    opengpts_user_id: Annotated[str, Cookie()], tid: str, payload: ThreadPayload
):
    return put_thread(
        opengpts_user_id,
        tid,
        assistant_id=payload["assistant_id"],
        name=payload["name"],
    )


app.mount("", StaticFiles(directory=str(ROOT / "ui"), html=True), name="ui")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8100)
