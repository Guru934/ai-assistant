import os
import subprocess
from cat_talker.logging_config import get_logger

logger = get_logger("cat_talker.agentic_tools")

# For scratchpad confirmation handling
SCRATCHPAD_PENDING_ACTIONS = {}

def read_file(path: str, offset: int = 0, limit: int = 2000) -> str:
    """Read a local file. Offset and limit control lines read."""
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return f"Error: File not found: {path}"
    try:
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            sliced = lines[offset:offset+limit]
            return "".join(sliced)
    except Exception as e:
        return f"Error reading file: {e}"

def write_file(action_id: str, path: str, content: str) -> str:
    """Destructive - requires UI approval."""
    path = os.path.expanduser(path)
    SCRATCHPAD_PENDING_ACTIONS[action_id] = {
        "type": "write_file",
        "path": path,
        "content": content,
        "description": f"Write to file: {path}"
    }
    return f"ACTION_PAUSED: Requested edit for {path}. Waiting for user confirmation. (ID: {action_id})"

def execute_bash(action_id: str, cmd: str, work_dir: str = "~") -> str:
    """Destructive - requires UI approval."""
    work_dir = os.path.expanduser(work_dir)
    SCRATCHPAD_PENDING_ACTIONS[action_id] = {
        "type": "execute_bash",
        "cmd": cmd,
        "work_dir": work_dir,
        "description": f"Execute in {work_dir}: `{cmd}`"
    }
    return f"ACTION_PAUSED: Requested command `{cmd}`. Waiting for user confirmation. (ID: {action_id})"

def ripgrep_search(query: str, path: str = "~") -> str:
    """Search for string recursively using rg."""
    path = os.path.expanduser(path)
    try:
        res = subprocess.run(["rg", "-n", query, path], capture_output=True, text=True, timeout=10)
        output = res.stdout
        if not output and res.stderr:
            return f"Error: {res.stderr}"
        if len(output) > 8000:
            return output[:8000] + "\n...[TRUNCATED]"
        return output if output else "No matches found."
    except FileNotFoundError:
        return "Error: ripgrep (rg) not installed on system."
    except subprocess.TimeoutExpired:
        return "Error: Search timed out."
    except Exception as e:
        return f"Error executing ripgrep: {e}"

def approve_action(action_id: str) -> str:
    if action_id not in SCRATCHPAD_PENDING_ACTIONS:
        return "Error: Action not found or already executed."
    
    action = SCRATCHPAD_PENDING_ACTIONS.pop(action_id)
    try:
        if action["type"] == "write_file":
            with open(action["path"], 'w', encoding='utf-8') as f:
                f.write(action["content"])
            return f"Successfully wrote to {action['path']}."
        
        elif action["type"] == "execute_bash":
            res = subprocess.run(action["cmd"], shell=True, cwd=action["work_dir"], capture_output=True, text=True)
            out = f"Exit code: {res.returncode}\n"
            if res.stdout:
                out += f"STDOUT:\n{res.stdout[:5000]}\n"
            if res.stderr:
                out += f"STDERR:\n{res.stderr[:5000]}"
            return out
            
    except Exception as e:
        return f"Error executing action: {e}"

def reject_action(action_id: str) -> str:
    if action_id in SCRATCHPAD_PENDING_ACTIONS:
        del SCRATCHPAD_PENDING_ACTIONS[action_id]
        return "Action rejected by user."
    return "Error: Action not found."

AGENTIC_TOOLS = [read_file, ripgrep_search]

