import sys
import asyncio
from utils.rate_limiter import RateLimiter
from services.elsa_core_service.llm import LLM
import os
import base64
from typing import AsyncGenerator, Dict, List, Union, Optional
import PyPDF2
from utils.helper_functions import parse_tags
from utils.enums import SYSTEMS, CoreResponseType
from .helper_functions import format_llm_messages, format_files_data
from .prompts import ELSA_RESPONSE_FORMAT_DESKTOP, ELSA_RESPONSE_FORMAT_PROMPT_MOBILE
from services.elsa_core_service.helper_functions import (
    process_llm_response,
    send_response_event,
)
from services.elsa_core_service.prompts import (
    get_system_prompt,
    get_advertisement_prompt,
)
from schemas.agent_interaction_request_schema import AgentInteractionRequest
import uuid
import json
from services.elsa_core_service.desktop_graph import ElsaDesktopAgent
from services.review_agent_service.review_graph import ReviewAgent
from services.elsa_core_service.call_graph import CallAgent
from starlette.concurrency import run_in_threadpool
from utils.logger import get_custom_logger
from logging import DEBUG

# from utils.invoke_desktop_tools import search_agent

logger = get_custom_logger("elsa_core_agent", level=DEBUG)

request_semaphore = asyncio.Semaphore(200)
background_task_queue = asyncio.Queue()
rate_limiter = RateLimiter(requests_per_minute=480)


# class AgentBase(LLM):
#     def __init__(self, system="mobile"):
#         super().__init__()
#         self.system = system


async def stream_agent_response(
    data: AgentInteractionRequest,
) -> AsyncGenerator[str, None]:
    async def send_error_response(error_message: str, session_id: str = None) -> str:
        error_data = {
            "type": "error",
            "message": error_message,
            "session_id": session_id,
        }
        return "data: " + json.dumps(error_data) + "\n\n"

    ## Rate limiting thing
    if not await rate_limiter.check_rate_limit(str(uuid.uuid4())):
        yield await send_error_response("Rate limit exceeded")
        return

    async with request_semaphore:
        try:
            user_current_question = data.user_current_question
            agent_name = data.agent_name
            agent_response = data.agent_response
            # files_data = data.files_data
            session_id = data.session_id or str(uuid.uuid4())
            supercharge = data.supercharge
            retrieval_content = data.retrieval_content
            context_history = data.context_history or []
            tools = data.tools or []
            roleplay = data.roleplay
            advertisement = data.advertisement
            datetime_local = data.datetime_local
            system = data.system  ## TODO: Call

            logger.info(f"Received Data: {data}")

            # files_data = format_files_data(files_data)

            try:
                if system == SYSTEMS.DESKTOP.value:
                    logger.info("Desktop System detected")

                    desktop_agent = ElsaDesktopAgent()

                    if agent_name:

                        review_agent = ReviewAgent()

                        response = review_agent.stream_graph_updates(
                            user_current_question=user_current_question,
                            agent_name=agent_name,
                            agent_result=json.dumps(agent_response),
                            context_history=context_history,
                        )
                        # logger.debug(response)
                        if response.get("response") and not response.get("code"):
                            # logger.debug(1)
                            response_data = {
                                "type": CoreResponseType.RESPONSE.value,
                                "response": response.get("response"),
                                "session_id": session_id,
                                "retry_attempt": response.get("retry_attempt"),
                            }

                            yield await send_response_event(response_data, session_id)

                            if response.get("search_sources"):
                                response_data = {
                                    "type": CoreResponseType.SEARCH_SOURCES.value,
                                    "serach_sources": response.get("search_sources"),
                                    "session_id": session_id,
                                    "retry_attempt": response.get("retry_attempt"),
                                }
                                yield await send_response_event(
                                    response_data, session_id
                                )

                        if response.get("fallback"):
                            response_data = {
                                "type": CoreResponseType.ERROR.value,
                                "feedback_response": response.get("fallback"),
                                "session_id": session_id,
                            }
                            yield await send_response_event(response_data, session_id)

                        elif response.get("suggested_agent"):
                            data = {
                                "type": CoreResponseType.AGENT_CALL.value,
                                "user_current_question": response.get(
                                    "user_current_question"
                                ),
                                "agent_name": response.get("suggested_agent"),
                                "session_id": session_id,
                                "core_feedback": response.get(
                                    "reason"
                                ),
                                "retry_attempt": response.get("retry_attempt"),
                            }
                            yield await send_response_event(data, session_id)

                    else:
                        response = desktop_agent.stream_graph_updates(
                            user_current_question,
                            context_history,
                            # files_data,
                            advertisement,
                        )

                        if response.get("response"):
                            response_data = {
                                "type": CoreResponseType.RESPONSE.value,
                                "response": response.get("response"),
                                "session_id": session_id,
                            }
                            yield await send_response_event(response_data, session_id)
                        elif response.get("agent"):
                            data = {
                                "type": CoreResponseType.AGENT_CALL.value,
                                # "user_query": response.get('user_query'),
                                "agent_name": response.get("agent"),
                                "session_id": session_id,
                            }
                            yield await send_response_event(data, session_id)

                elif system == SYSTEMS.MOBILE.value:
                    logger.info("Mobile System detected")
                    pass

                elif system == SYSTEMS.CALL.value:
                    logger.info("Call System detected")

                    call_agent = CallAgent()

                    # response = call_agent.stream_graph_updates(context_history, user_current_question)
                    # if response.get("response"):
                    #     response_data = {
                    #         "type":CoreResponseType.RESPONSE.value,
                    #         "response": response.get("response"),
                    #         "interruption_flag": response.get("interruption_flag"),
                    #         "session_id": session_id,
                    #     }
                    #     yield await send_response_event(response_data, session_id)

                    async for response in call_agent.stream_graph_updates(context_history, user_current_question):

                        if response["type"] == "chunk":
                            response_data = {
                                "type": CoreResponseType.RESPONSE.value,
                                "response": response.get("content"),
                                "session_id": session_id,
                            }
                            yield await send_response_event(response_data, session_id)
                        elif response["type"] == "interruption":
                            response_data = {
                                "type": CoreResponseType.INTERRUPTION.value,
                                "interruption_flag": response.get("interruption_flag"),
                                "session_id": session_id,
                            }
                            yield await send_response_event(response_data, session_id)

                else:
                    yield await send_error_response(error_message="Invalid system. Please check the system field in the request.", session_id=session_id)
                    return

            except Exception as e:
                logger.error(f"Error processing request: {str(e)}", exc_info=True)
                yield await send_response_event(
                    {
                        "type":  CoreResponseType.ERROR.value,
                        "response": "An unexpected error occurred",
                        "session_id": session_id,
                    },
                    session_id,
                )

            yield "data: " + json.dumps(
                {"type": "complete", "session_id": session_id}
            ) + "\n\n"

        except Exception as e:
            logger.error(f"Error in Elsa Processing: {str(e)}", exc_info=True)
            yield await send_error_response(f"Error in Elsa Processing: {str(e)}")


async def process_background_tasks():
    while True:
        task = await background_task_queue.get()
        asyncio.create_task(task)
        background_task_queue.task_done()
