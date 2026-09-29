import asyncio
import time
import os
import random

from google import genai
from google.genai import types

from cat_talker.tools import ALL_TOOLS
from cat_talker.audio import AudioInterface
from cat_talker.vision import VisionInterface
from cat_talker.logging_config import get_logger
from cat_talker.earcons import play_earcon

logger = get_logger("cat_talker.agent")

# --- MONKEY PATCH WEBSOCKETS TO DISABLE PING TIMEOUTS FOR STRICT PROXIES ---
import websockets.asyncio.client
_orig_connect = websockets.asyncio.client.connect
def _patched_connect(*args, **kwargs):
    kwargs['ping_interval'] = 20.0
    kwargs['ping_timeout'] = 20.0
    return _orig_connect(*args, **kwargs)
websockets.asyncio.client.connect = _patched_connect
# --------------------------------------------------------------------------

class GeminiDesktopAgent:
    def __init__(self):
        self.client = genai.Client()
        self.audio = None
        self.vision = None
        self.stop_event = asyncio.Event()
        self._is_speaking = False
        self._is_processing = False
        self.synthetic_input_queue = asyncio.Queue()
        self.loop = None

    def _set_state(self, state_callback, state: str):
        if state_callback:
            state_callback(state)

    def _set_bubble(self, bubble_callback, text: str):
        if bubble_callback:
            bubble_callback(text)

    def _set_glow(self, glow_callback, state: str):
        if glow_callback:
            glow_callback(state)

    async def run_loop(self, volume_callback=None, app_quit_callback=None, text_callback=None,
                       state_callback=None, bubble_callback=None, glow_callback=None):
        self.loop = asyncio.get_running_loop()
        self.audio = AudioInterface()
        self.audio.volume_cb = volume_callback
        
        self.vision = VisionInterface()

        model = "gemini-3.1-flash-live-preview"
        logger.info(f"Connecting to Gemini Live API with model: {model}")

        system_instructions = (
            "You are 'Chibi', a cheerful, cute, and ultra-helpful desktop AI companion. "
            "You have direct access to the user's computer via tools! You can open apps, open websites in browser, "
            "read the clipboard (including currently highlighted text via primary_selection=True), check the active window, set the volume, set brightness, take screenshots, "
            "control media, switch workspaces, and send notifications. "
            "YOU HAVE VISION ON DEMAND - when the user asks you to look at something, use the take_screenshot tool "
            "to capture the screen and analyze it. "
            f"To click something on the screen, intelligently guess the X, Y coordinate based on the exact screen "
            f"resolution of {self.vision.monitor_width}x{self.vision.monitor_height}. You MUST output absolute pixel "
            "coordinates mapping to this grid and use the click_screen(x,y) tool. "
            "When asked to open something or perform an OS task, ALWAYS execute the appropriate tool function. "
            "Never say you cannot see or control the PC. Use your tools immediately to fulfill the request! "
            "If the user asks to format/fix highlighted text, use get_clipboard(primary_selection=True), process it, and use set_clipboard(text) to copy the result."
        )

        try:
            while not self.stop_event.is_set():
                try:
                    import cat_talker.tools
                    dynamic_instructions = system_instructions
                    if cat_talker.tools.PENDING_RISKY_ACTION:
                        dynamic_instructions += f"\n\n[SYSTEM MEMORY RECOVERY]: Your connection just dropped and you forgot the last few seconds. Right before you dropped, you asked the user for permission to run `{cat_talker.tools.PENDING_RISKY_ACTION['name']}`. If the user says \"yes\" or gives you permission right now, YOU MUST IMMEDIATELY CALL THE `confirm_action` TOOL to execute it!"
                    
                    config = types.LiveConnectConfig(
                        response_modalities=["AUDIO"],
                        system_instruction=types.Content(parts=[types.Part(text=dynamic_instructions)]),
                        output_audio_transcription=types.AudioTranscriptionConfig(word_timestamp=False),
                        tools=ALL_TOOLS
                    )

                    async with self.client.aio.live.connect(model=model, config=config) as session:
                        logger.info("====================================")
                        logger.info("✅ Session established securely!")
                        logger.info("🎙️ Speak into your microphone now...")
                        logger.info("====================================")
                        if text_callback:
                            text_callback("system", "Connected! Speak now...")
                        self._set_state(state_callback, "idle")
                        self._set_glow(glow_callback, "connected")
                        
                        # Reset reconnect attempts on successful connection
                        self._reconnect_attempts = 0

                        send_lock = asyncio.Lock()

                        async def mic_worker():
                            logger.info("Started Mic Stream...")
                            while not self.stop_event.is_set():
                                chunk = await self.audio.audio_in_queue.get()
                                try:
                                    async with send_lock:
                                        await session.send_realtime_input(
                                            audio=types.Blob(data=chunk, mime_type='audio/pcm;rate=16000')
                                        )
                                except Exception as e:
                                    logger.error(f"Mic send error (reconnecting): {e}", exc_info=True)
                                    break

                        async def receive_worker():
                            try:
                                async for msg in session.receive():
                                    if msg.server_content:
                                        if hasattr(msg.server_content, "interrupted") and msg.server_content.interrupted:
                                            self.audio.clear_output_queue()
                                            self._is_speaking = False
                                            self._set_state(state_callback, "listening")
                                            import cat_talker.tools
                                            cat_talker.tools.stop_media_ducking()

                                        if hasattr(msg.server_content, 'turn_complete') and msg.server_content.turn_complete:
                                            self._is_speaking = False
                                            self._set_state(state_callback, "listening")
                                            self._set_glow(glow_callback, "connected")
                                            import cat_talker.tools
                                            cat_talker.tools.stop_media_ducking()

                                        if msg.server_content.model_turn:
                                            # Model started responding - thinking phase
                                            if not self._is_speaking:
                                                self._is_speaking = True
                                                self._set_state(state_callback, "thinking")
                                                self._set_glow(glow_callback, "thinking")
                                                import cat_talker.tools
                                                cat_talker.tools.start_media_ducking()

                                            for part in msg.server_content.model_turn.parts:
                                                if hasattr(part, "inline_data") and part.inline_data:
                                                    # Audio chunk arriving - speaking phase
                                                    if not self._is_speaking:
                                                        self._is_speaking = True
                                                    self._set_state(state_callback, "talking")
                                                    self._set_glow(glow_callback, "connected")
                                                    self.audio.queue_output(part.inline_data.data)
                                                if hasattr(part, "text") and part.text and text_callback:
                                                    text_callback("model", part.text)
                                                    # Also show in bubble
                                                    self._set_bubble(bubble_callback, part.text)

                                    if hasattr(msg, "client_content") and msg.client_content:
                                        for turn in getattr(msg.client_content, "turns", []):
                                            if turn.role == "user":
                                                for part in turn.parts:
                                                    if hasattr(part, "text") and part.text and text_callback:
                                                        text_callback("user", part.text)
                                                        self._set_state(state_callback, "listening")
                                                        self._set_glow(glow_callback, "connected")

                                    if hasattr(msg, "tool_call") and msg.tool_call:
                                        from cat_talker.tools import ALL_TOOLS

                                        tool_func_map = {func.__name__: func for func in ALL_TOOLS}

                                        responses = []
                                        for function_call in msg.tool_call.function_calls:
                                            result_dict = {"error": "Function not found"}
                                            if function_call.name in tool_func_map:
                                                func = tool_func_map[function_call.name]
                                                try:
                                                    args = function_call.args if hasattr(function_call, "args") and function_call.args else {}

                                                    # Show processing state for tool calls
                                                    if function_call.name in {"click_screen", "type_text", "press_key", 
                                                        "open_application", "open_website", "search_and_play_youtube",
                                                        "set_volume", "set_brightness", "take_screenshot"}:
                                                        self._set_state(state_callback, "thinking")
                                                        self._set_glow(glow_callback, "processing")

                                                    start_time = time.time()
                                                    if isinstance(args, dict):
                                                        result = func(**args)
                                                    else:
                                                        result = func()
                                                    duration = time.time() - start_time
                                                    
                                                    # Send notification if task took more than 3 seconds
                                                    if duration > 3.0:
                                                        from cat_talker.tools import send_notification
                                                        res_str = str(result)
                                                        if len(res_str) > 100: res_str = res_str[:97] + "..."
                                                        send_notification("Task Completed", f"{function_call.name}: {res_str}")

                                                    # Handle vision-on-demand for take_screenshot
                                                    if function_call.name == "take_screenshot" and isinstance(result, str) and "Saved screenshot" in result:
                                                        self._set_glow(glow_callback, "vision")
                                                        # Capture frame and send to Gemini for analysis
                                                        try:
                                                            monitor_arg = args.get("monitor", "") if isinstance(args, dict) else ""
                                                            frame = self.vision.capture_frame(monitor=monitor_arg)
                                                            if frame:
                                                                async with send_lock:
                                                                    await session.send_realtime_input(
                                                                        video=types.Blob(data=frame, mime_type='image/jpeg')
                                                                    )
                                                            self._set_bubble(bubble_callback, "📸 Screenshot captured and sent to Gemini")
                                                        except Exception as e:
                                                            logger.error(f"Screenshot send error: {e}", exc_info=True)
                                                        # For now, the model will respond based on the tool result text
                                                    if function_call.name == "inspect_screen":
                                                        try:
                                                            monitor_arg = args.get("monitor", "") if isinstance(args, dict) else ""
                                                            frame = self.vision.capture_frame(monitor=monitor_arg)
                                                            if frame:
                                                                async with send_lock:
                                                                    await session.send_realtime_input(
                                                                        video=types.Blob(data=frame, mime_type='image/jpeg')
                                                                    )
                                                                self._set_bubble(bubble_callback, "📸 Screen analyzed by Gemini")
                                                        except Exception as e:
                                                            logger.error(f"Inspect screen error: {e}", exc_info=True)

                                                    # Update UI depending on outcome
                                                    if isinstance(result, str) and "You MUST verbally ask the user for permission" in result:
                                                        if text_callback:
                                                            text_callback("system", f"⚠️ WAITING FOR VOICE APPROVAL: {function_call.name}")
                                                        logger.warning(f"VOICE APPROVAL REQUIRED: Chibi wants to {function_call.name} with args {args}")
                                                    else:
                                                        if text_callback:
                                                            text_callback("system", f"🛠️ Executed {function_call.name}: {result}")
                                                        logger.info(f"🛠️ Executed {function_call.name}: {result}")

                                                    result_dict = {"result": result}
                                                except Exception as err:
                                                    result_dict = {"error": str(err)}

                                            responses.append(types.FunctionResponse(
                                                id=function_call.id,
                                                name=function_call.name,
                                                response=result_dict
                                            ))

                                        if responses:
                                            async with send_lock:
                                                await session.send_tool_response(function_responses=responses)
                                            
                                            # Return to listening after tool execution
                                            self._set_state(state_callback, "listening")
                                            self._set_glow(glow_callback, "connected")

                            except Exception as e:
                                logger.error(f"Receive interrupted (reconnecting): {e}", exc_info=True)


                        async def synthetic_input_worker():
                            logger.info("Started Synthetic Input Worker...")
                            while not self.stop_event.is_set():
                                try:
                                    action = await self.synthetic_input_queue.get()
                                    if action == "ACTIVE_WINDOW":
                                        region = self.vision.get_active_window_region()
                                        frame = self.vision.capture_frame(region=region)
                                        if frame:
                                            self._set_bubble(bubble_callback, "📸 Captured Active Window")
                                            self._set_glow(glow_callback, "vision")
                                            async with send_lock:
                                                # Send the image
                                                await session.send_realtime_input(video=types.Blob(data=frame, mime_type='image/jpeg'))
                                                # Send text prompt
                                                try:
                                                    await session.send(input="The user just pressed the active window hotkey. Look at the provided image. What do you see? Or ask the user how you can help with it.")
                                                except AttributeError:
                                                    # Fallback if send doesn't accept input= kwarg
                                                    req = types.LiveClientContent(
                                                        turns=[types.Content(role="user", parts=[types.Part(text="The user just pressed the active window hotkey. Look at the provided image. What do you see?")])]
                                                    )
                                                    await session.send_client_content(req)
                                except Exception as e:
                                    logger.error(f"Synthetic input worker error: {e}", exc_info=True)

                        tasks = [
                            asyncio.create_task(mic_worker()),
                            asyncio.create_task(receive_worker()),
                            asyncio.create_task(synthetic_input_worker())
                        ]

                        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                        for task in pending:
                            task.cancel()

                except Exception as e:
                    logger.error(f"Agent connection dropped (auto-reconnecting): {e}", exc_info=True)
                    play_earcon("fail")
                    if text_callback:
                        text_callback("system", f"Reconnecting... ({e})")
                    
                    # Exponential backoff with jitter
                    if not hasattr(self, '_reconnect_attempts'):
                        self._reconnect_attempts = 0
                    self._reconnect_attempts += 1
                    max_delay = 60  # Max 60 seconds
                    base_delay = min(2 ** self._reconnect_attempts, max_delay)
                    jitter = random.uniform(0, 1)
                    delay = base_delay + jitter
                    logger.info(f"Reconnecting in {delay:.1f}s (attempt {self._reconnect_attempts})...")
                    await asyncio.sleep(delay)
                    
                    # Reset reconnect attempts on successful connection (handled in the while loop restart)

        finally:
            logger.info("Shutting down audio...")
            if self.audio:
                self.audio.close()
                self.audio = None
            if app_quit_callback:
                app_quit_callback()

def start_agent_in_thread(volume_cb, quit_cb=None, text_cb=None, state_cb=None, bubble_cb=None, glow_cb=None, global_agent_ref=None):
    agent = GeminiDesktopAgent()
    if global_agent_ref is not None:
        global_agent_ref.append(agent)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(agent.run_loop(volume_cb, quit_cb, text_cb, state_cb, bubble_cb, glow_cb))
    except KeyboardInterrupt:
        pass
    finally:
        agent.stop_event.set()
        loop.close()
