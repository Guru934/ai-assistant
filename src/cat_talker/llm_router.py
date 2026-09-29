import requests
import json
import os
from typing import Generator
from google import genai

from cat_talker.agentic_tools import AGENTIC_TOOLS, write_file, execute_bash, read_file, ripgrep_search
from cat_talker.tools import list_directory

SYSTEM_PROMPT = """You are Cat Talker Workspace Agent.
You can read/write files and execute bash commands to help the user.
To execute bash, output exactly:
<bash>command here</bash>

To write a file, output exactly:
<write path="path/to/file">content here</write>

To read a file, output exactly:
<read>path/to/file</read>

To search via ripgrep, output exactly:
<search>query here</search>

Do not execute multiple tools in a single response block. Stop after one tool!
"""

def get_installed_models():
    models = ["gemini-3.1-flash-live-preview"]
    try:
        res = requests.get("http://localhost:11434/api/tags", timeout=1)
        if res.status_code == 200:
            models.extend([m["name"] for m in res.json().get("models", [])])
    except:
        pass
    return models

def chat_completion_stream(model_id: str, messages: list) -> Generator[str, None, None]:
    # Inject system prompt
    formatted_msgs = [{"role": "system", "content": SYSTEM_PROMPT}] + messages
    
    if model_id != "gemini-3.1-flash-live-preview" and "gemini" not in model_id.lower():
        try:
            res = requests.post(
                "http://localhost:11434/api/chat",
                json={"model": model_id, "messages": formatted_msgs, "stream": True},
                stream=True
            )
            for line in res.iter_lines():
                if line:
                    data = json.loads(line.decode('utf-8'))
                    if "message" in data and "content" in data["message"]:
                        yield data["message"]["content"]
        except Exception as e:
            yield f"\n[System Error: {str(e)}]"
    else:
        try:
            client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))
            gemini_msgs = []
            for m in messages:
                role = "model" if m["role"] == "assistant" else "user"
                gemini_msgs.append({"role": role, "parts": [{"text": m["content"]}]})
            
            response = client.models.generate_content_stream(
                model=model_id,
                contents=gemini_msgs,
                config={"system_instruction": [{"text": SYSTEM_PROMPT}]}
            )
            for chunk in response:
                yield chunk.text
        except Exception as e:
            yield f"\n[System Error: {str(e)}]"
