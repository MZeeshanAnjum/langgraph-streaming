import base64
from io import BytesIO
from typing import List, Dict
from langgraph.graph import StateGraph, START, END
from langchain_core.messages import SystemMessage
from services.elsa_core_service.llm import llm
from services.elsa_core_service.call_prompts import CALL_AGENT_PROMPT, INTERRUPTION_AGENT_PROMPT
from schemas.call_schema import State, CallAgentResponse, InterruptionAgentResponse
from config import MAX_MESSAGE_HISTORY
import asyncio
from logging import DEBUG
from utils.logger import get_custom_logger

logger = get_custom_logger("call_llm_agent", level=DEBUG)

class CallAgent:
    def __init__(self):
        self.llm = llm
        self.system_prompt_template = CALL_AGENT_PROMPT
        self.interruption_agent_prompt=INTERRUPTION_AGENT_PROMPT
        self.graph = self._build_graph()

    async def agent_response(self, state: State):
        # Trim to last 6 messages (3 user + 3 assistant) if necessary
        full_history = state.context_history
        if len(full_history) > MAX_MESSAGE_HISTORY:
            # Keep only last 3 user + 3 assistant messages (i.e., last 6 entries)
            context = full_history[-MAX_MESSAGE_HISTORY:]
        else:
            context = full_history

        # Convert to LangChain message objects for LLM
        chat_msgs = []
        for msg in context:
            role = msg["role"]
            content = msg["content"]
            if role == "user":
                from langchain_core.messages import HumanMessage
                chat_msgs.append(HumanMessage(content=content))
            elif role == "assistant":
                from langchain_core.messages import AIMessage
                chat_msgs.append(AIMessage(content=content))

        # Format chat history for prompt
        formatted_history = "\n".join([f"{m['role'].capitalize()}: {m['content']}" for m in context])
        prompt = self.system_prompt_template.format(
            context_history=formatted_history,
            user_current_question=state.user_current_question
        )
        messages = [SystemMessage(content=prompt)]
        # llm_structured = self.llm.with_structured_output(CallAgentResponse)
        # response= llm_structured.ainvoke(messages)
        # logger.info(response)
        # updated_history = full_history + [
        #     {"role": "user", "content": state.user_current_question},
            # {"role": "assistant", "content": response.get("response")}]

        # logger.info(updated_history)

        # return {
        #     "context_history": updated_history,
        #     "response": response.get("response")
        # }
        assistant_content = ""
        messages = [SystemMessage(content=prompt)]
        async for chunk in self.llm.astream(messages):
            # logger.info(chunk.content)

            # Ensure chunk is not None and has 'response'
            token = chunk.content
            assistant_content += token

            if token.strip():
                yield {"response": token}

        updated_history = full_history + [
        {"role": "user", "content": state.user_current_question},
        {"role": "assistant", "content": assistant_content}
        ]

        yield {"context_history": updated_history} 

    async def interruption_agent(self, state: State):
        # Trim to last 6 messages (3 user + 3 assistant) if necessary
        full_history = state.context_history
        if len(full_history) > MAX_MESSAGE_HISTORY:
            # Keep only last 3 user + 3 assistant messages (i.e., last 6 entries)
            context = full_history[-MAX_MESSAGE_HISTORY:]
        else:
            context = full_history

        formatted_history = "\n".join([f"{m['role'].capitalize()}: {m['content']}" for m in context])
        prompt = self.interruption_agent_prompt.format(
            context_history=formatted_history,
            user_current_question=state.user_current_question
        )

        messages = [SystemMessage(content=prompt)]
        llm_structured = self.llm.with_structured_output(InterruptionAgentResponse)

        response = await asyncio.to_thread(llm_structured.invoke, messages)
        logger.info(response)
        yield {"interruption_flag": response.get("interruption_flag")}



        

    

    def _build_graph(self):
        builder = StateGraph(State)
        builder.add_node("agent response", self.agent_response)
        builder.add_node("interruption agent",self.interruption_agent)
        builder.add_edge(START, "interruption agent")
        builder.add_edge( "interruption agent", "agent response")
        builder.add_edge("agent response", END)
        return builder.compile()

    async def stream_graph_updates(self, context_history: List[Dict[str, str]], user_current_question: str):
    # def stream_graph_updates(self, context_history: List[Dict[str, str]], user_current_question: str) -> dict:
        state_input = {
            "context_history": context_history,
            "user_current_question": user_current_question,
            # "interruption_flag": None
        }

        interruption_sent = False

        # final_context_history = context_history
        # responses = {}
        # for event in self.graph.stream(state_input):
        #     for step, value in event.items():
        #         responses[step] = value

        # logger.info(responses)
        
        # latest_assistant_msg = next((msg["content"] for msg in reversed(responses["agent response"]["context_history"]) if msg["role"] == "assistant"))
        # return {"response": latest_assistant_msg, "interruption_flag" : responses["interruption agent"]["interruption_flag"] } 

        async for event in self.graph.astream_events(state_input, version="v2"):
            if event["event"] == "on_chain_stream":
                chunk = event["data"]["chunk"]

                # Stream interruption flag first
                if not interruption_sent and "interruption_flag" in chunk and chunk["interruption_flag"] is not None :
                    yield {"type": "interruption", "interruption_flag": chunk["interruption_flag"]}
                    interruption_sent = True

                # Then stream LLM response
                if "response" in chunk:
                    yield {"type": "chunk", "content": chunk["response"]}

            # elif event["event"] == "on_chain_end":
            #     output = event["data"].get("output", {})
            #     final_context_history = output.get("context_history", context_history)
            #     interruption_flag = output.get("interruption_flag", False)

            #     if not interruption_sent:
            #         yield {"type": "interruption", "interruption_flag": interruption_flag}

            #     yield {
            #         "type": "final",
            #         "response": assistant_response,

            #     }


    # async def run(self):
    def run(self): 
        context_history = []
        while True:
            user_current_question = input("\nYou: ")
            if user_current_question.strip() == "1":
                break
            response = self.stream_graph_updates(context_history, user_current_question)
            # last_msg = context_history[-1] if context_history else None


            # last_msg = None
            # final_context_history = []

            # async for event in self.stream_graph_updates(context_history, user_current_question):
            #     if event["type"] == "chunk":
            #         logger.info(event["content"])  # stream token live
            #     elif event["type"] == "final":
            #         final_context_history = event["context_history"]
            #         last_msg = final_context_history[-1] if final_context_history else None


            # context_history = final_context_history  # update state for next turn

            if response.get("response"):
                logger.info(f"\n\nELSA: {response}")

    def visualize(self):
        logger.info("Generating graph visualization")
        png_image = self.graph.get_graph(xray=True).draw_mermaid_png()
        image_buffer = BytesIO(png_image)
        img_str = base64.b64encode(image_buffer.getvalue()).decode("utf-8")
        return {"selection_agent_base64": f"data:image/png;base64,{img_str}"}

 
if __name__ == "__main__":
    agent = CallAgent()
    asyncio.run(agent.visualize())
