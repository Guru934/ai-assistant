import asyncio
import threading
import time
import json
import os
import random

import websockets
from google import genai
from google.genai import types

from cat_talker.tools import ALL_TOOLS
from cat_talker.audio import AudioInterface
from cat_talker.vision import VisionInterface
from cat_talker.logging_config import get_logger
from cat_talker.earcons import play_earcon

logger = get_logger("cat_talker.agent")


# Tools that change world state (apps, browser, volume, screen, clipboard,
# pending risky actions, ...). Repeating an identical call within one user
# interaction is never useful, so these get semantic side-effect dedup on
# top of function_call.id dedup. Pure readers are exempt: get_clipboard,
# get_active_window, list_directory, inspect_screen.
SIDE_EFFECT_TOOLS = frozenset({
    "open_application", "open_website", "open_file",
    "set_volume", "set_brightness", "take_screenshot",
    "search_and_play_youtube", "focus_or_launch", "switch_workspace",
    "media_action", "set_clipboard", "send_notification",
    "confirm_action", "cancel_action",
    "click_screen", "type_text", "press_key",
    "save_user_preference",
})


def canonical_tool_signature(name, args):
    """Canonical side-effect signature: tool_name + canonical JSON(args).

    Identical side effects produce identical strings regardless of dict
    ordering; anything unserializable falls back to repr so dedup never
    crashes the worker.
    """
    try:
        if isinstance(args, dict):
            canon = json.dumps(args, sort_keys=True, separators=(",", ":"),
                               default=str)
        elif args in (None, {}, ""):
            canon = ""
        else:
            canon = repr(args)
    except Exception:
        canon = repr(args)
    return f"{name}({canon})"

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
        # run_loop task, set by start_agent_in_thread
        self._run_task: asyncio.Task | None = None
        # Explicit session/connection state so the mic only feeds the active session
        self._session_active = asyncio.Event()
        self._session_generation = 0  # incremented per new session
        self._reconnect_attempts = 0
        # Mic input observability (counters only; updated by mic_worker).
        self._mic_stats = {"received": 0, "sent": 0, "dropped_stale": 0}
        # Seconds of mic silence after which mic_worker warns that the input
        # queue may be starved (dead device / stalled stream). Lower in tests.
        self._mic_quiet_timeout = 5.0
        # Output-level mic suppression epoch: bumped every time the model
        # starts responding. Drain threads release the mic only if no newer
        # output has started since. Cooldown after drain, before re-enable.
        self._output_epoch = 0
        self._mic_release_cooldown = 0.5

    def _set_state(self, state_callback, state: str):
        if state_callback:
            state_callback(state)

    def _set_bubble(self, bubble_callback, text: str):
        if bubble_callback:
            bubble_callback(text)

    def _set_glow(self, glow_callback, state: str):
        if glow_callback:
            glow_callback(state)

    def _clear_input_queue(self):
        """Drain stale microphone audio so a new session cannot replay old speech."""
        if self.audio is None:
            return
        drained = 0
        while not self.audio.audio_in_queue.empty():
            try:
                self.audio.audio_in_queue.get_nowait()
                self.audio.audio_in_queue.task_done()
                drained += 1
            except asyncio.QueueEmpty:
                break
        if drained:
            logger.info(f"Flushed {drained} stale mic chunks before new session")

    def _suppress_mic_for_output(self):
        """Model started responding: mute the mic for the whole turn.

        Output-level suppression (half-duplex): capture stays muted across
        ALL audio chunks until _release_mic_after_turn runs. Never toggled
        per chunk.
        """
        self._output_epoch += 1
        if self.audio is not None:
            self.audio.suppress_mic()

    def request_stop(self):
        """Thread-safe shutdown request (SIGINT handler / Qt aboutToQuit).

        Sets the stop flag and cancels the run_loop task so even a stuck
        worker (e.g. a long reconnect backoff) unwinds promptly. Safe to
        call from any thread, multiple times.
        """
        self.stop_event.set()
        loop = self.loop
        task = self._run_task
        if loop is not None and task is not None and not task.done():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # loop already closed

    def _release_mic_after_turn(self):
        """Model turn finished: re-enable the mic once pending assistant
        output has drained, plus a short cooldown.

        Runs in a daemon thread so receive_worker never blocks. Aborts if a
        newer model response starts first or the agent is stopping.
        """
        epoch = self._output_epoch
        audio = self.audio

        def _wait():
            if audio is None:
                return
            while not audio.audio_out_queue.empty():
                if epoch != self._output_epoch or self.stop_event.is_set():
                    return
                time.sleep(0.05)
            waited = 0.0
            while waited < self._mic_release_cooldown:
                time.sleep(0.05)
                waited += 0.05
                if epoch != self._output_epoch or self.stop_event.is_set():
                    return
            audio.release_mic()

        threading.Thread(target=_wait, daemon=True).start()

    async def run_loop(self, volume_callback=None, app_quit_callback=None, text_callback=None,
                       state_callback=None, bubble_callback=None, glow_callback=None):
        self.loop = asyncio.get_running_loop()
        # Reuse the existing AudioInterface (hardware) across reconnects instead
        # of creating a new one per session.
        if self.audio is None:
            self.audio = AudioInterface()
        self.audio.volume_cb = volume_callback

        if self.vision is None:
            self.vision = VisionInterface()

        model = "gemini-3.8-live"
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
                        input_audio_transcription=types.AudioTranscriptionConfig(),
                        tools=ALL_TOOLS
                    )

                    # SDK lifecycle: connect() returns an async context manager.
                    # Leaving the block closes the Live session; reconnects
                    # create a brand-new block (and brand-new workers).
                    async with self.client.aio.live.connect(model=model, config=config) as session:
                        # Clear any stale mic audio queued during the previous
                        # connection, and gate the microphone onto this session.
                        self._clear_input_queue()
                        self._session_generation += 1
                        self._session_active.set()

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

                        # Per-session tool-call idempotency: the model may
                        # re-send a function_call (e.g. after hearing its own
                        # spoken confirmation come back through the mic). A
                        # given function_call.id executes at most once per
                        # session; repeats reuse the first result so the side
                        # effect never runs twice while the protocol (one
                        # response per call) is still honored.
                        seen_tool_call_ids = {}
                        # Per-interaction semantic dedup: identical side
                        # effects (same tool + canonical args) run once per
                        # user turn even under different IDs. Reset whenever
                        # a new user turn starts (input transcription or user
                        # text below); survives tool-response continuations
                        # and turn_complete so a confused model cannot
                        # re-trigger a side effect without new user input.
                        seen_signatures = {}
                        self._interaction_id = 0
                        mic_baseline = dict(self._mic_stats)

                        async def mic_worker():
                            logger.info("Started Mic Stream...")
                            gen = self._session_generation
                            stats = self._mic_stats
                            try:
                                while not self.stop_event.is_set():
                                    try:
                                        chunk = await asyncio.wait_for(
                                            self.audio.audio_in_queue.get(),
                                            timeout=self._mic_quiet_timeout,
                                        )
                                    except asyncio.TimeoutError:
                                        # Diagnostic: the queue has been silent for
                                        # a while - mic device dead or stream stalled.
                                        logger.warning(
                                            "mic_worker: no microphone input for %.1fs "
                                            "(received=%d sent=%d dropped_stale=%d) - "
                                            "check mic device / AudioInterface stream",
                                            self._mic_quiet_timeout,
                                            stats["received"], stats["sent"],
                                            stats["dropped_stale"],
                                        )
                                        continue
                                    stats["received"] += 1
                                    if stats["received"] % 50 == 0:
                                        logger.debug(
                                            "mic_worker input: received=%d sent=%d dropped_stale=%d",
                                            stats["received"], stats["sent"],
                                            stats["dropped_stale"],
                                        )
                                    # Only feed the currently active session: drop chunks
                                    # queued for a session that has already been replaced.
                                    if gen != self._session_generation or not self._session_active.is_set():
                                        stats["dropped_stale"] += 1
                                        logger.info("mic_worker dropped stale chunk (session changed)")
                                        continue
                                    try:
                                        async with send_lock:
                                            await session.send_realtime_input(
                                                audio=types.Blob(data=chunk, mime_type='audio/pcm;rate=16000')
                                            )
                                        stats["sent"] += 1
                                    except asyncio.CancelledError:
                                        raise
                                    except Exception as e:
                                        logger.error(f"Mic send error (reconnecting): {e}", exc_info=True)
                                        raise
                            except asyncio.CancelledError:
                                raise
                            finally:
                                logger.warning("mic_worker exited")

                        async def receive_worker():
                            # NOTE: one session.receive() async-iterator is ONE
                            # interaction, not the session lifetime. Normal
                            # exhaustion just ends the turn: loop back and call
                            # receive() again. Only real errors (or shutdown)
                            # leave this worker.
                            suppressed_signatures = 0
                            try:
                                while not self.stop_event.is_set():
                                    suppressed_signatures = 0
                                    async for msg in session.receive():
                                        if msg.server_content:
                                            if hasattr(msg.server_content, "interrupted") and msg.server_content.interrupted:
                                                self.audio.clear_output_queue()
                                                self._is_speaking = False
                                                self._set_state(state_callback, "listening")
                                                import cat_talker.tools
                                                cat_talker.tools.stop_media_ducking()
                                                # Model cut off: output already
                                                # cleared, so drain is instant;
                                                # cooldown still applies.
                                                self._release_mic_after_turn()

                                            if hasattr(msg.server_content, 'turn_complete') and msg.server_content.turn_complete:
                                                self._is_speaking = False
                                                self._set_state(state_callback, "listening")
                                                self._set_glow(glow_callback, "connected")
                                                import cat_talker.tools
                                                cat_talker.tools.stop_media_ducking()
                                                # Turn finished: mic back on after
                                                # pending output drains + cooldown.
                                                self._release_mic_after_turn()

                                            # User speech transcription (requires
                                            # input_audio_transcription in the session
                                            # config). Observability for the mic path:
                                            # proves user audio reached Gemini.
                                            input_trans = getattr(msg.server_content, "input_transcription", None)
                                            if input_trans is not None:
                                                input_text = getattr(input_trans, "text", None)
                                                if input_text:
                                                    logger.info(f"🎤 User said: {input_text}")
                                                    if text_callback:
                                                        text_callback("user", input_text)
                                                    # New user turn: semantic dedup
                                                    # starts over (ID dedup stays
                                                    # per-session by design).
                                                    self._interaction_id += 1
                                                    seen_signatures.clear()

                                            if msg.server_content.model_turn:
                                                # Model started responding - thinking phase
                                                if not self._is_speaking:
                                                    self._is_speaking = True
                                                    self._set_state(state_callback, "thinking")
                                                    self._set_glow(glow_callback, "thinking")
                                                    import cat_talker.tools
                                                    cat_talker.tools.start_media_ducking()
                                                    # Output-level mic suppression
                                                    # for the whole turn.
                                                    self._suppress_mic_for_output()

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
                                                    # Text user turn: same reset
                                                    # as voice transcription.
                                                    self._interaction_id += 1
                                                    seen_signatures.clear()

                                        if hasattr(msg, "tool_call") and msg.tool_call:
                                            from cat_talker.tools import ALL_TOOLS

                                            tool_func_map = {func.__name__: func for func in ALL_TOOLS}

                                            responses = []
                                            for function_call in msg.tool_call.function_calls:
                                                call_id = getattr(function_call, "id", None)
                                                args = function_call.args if hasattr(function_call, "args") and function_call.args else {}
                                                sig = canonical_tool_signature(function_call.name, args)
                                                result_dict = {"error": "Function not found"}
                                                if call_id is not None and call_id in seen_tool_call_ids:
                                                    logger.debug(
                                                        "🛠️ Tool call interaction=%d name=%s id=%s sig=%s "
                                                        "classification=DUPLICATE_ID (already executed; "
                                                        "reusing first result)",
                                                        self._interaction_id, function_call.name,
                                                        call_id, sig,
                                                    )
                                                    result_dict = seen_tool_call_ids[call_id]
                                                elif (function_call.name in SIDE_EFFECT_TOOLS
                                                        and sig in seen_signatures):
                                                    suppressed_signatures += 1
                                                    logger.debug(
                                                        "🛠️ Tool call interaction=%d name=%s id=%s sig=%s "
                                                        "classification=DUPLICATE_SIGNATURE (same side "
                                                        "effect already ran this turn; reusing result)",
                                                        self._interaction_id, function_call.name,
                                                        call_id, sig,
                                                    )
                                                    result_dict = seen_signatures[sig]
                                                elif function_call.name in tool_func_map:
                                                    logger.info(
                                                        "🛠️ Tool call interaction=%d name=%s id=%s sig=%s "
                                                        "classification=NEW",
                                                        self._interaction_id, function_call.name,
                                                        call_id, sig,
                                                    )
                                                    func = tool_func_map[function_call.name]
                                                    try:

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

                                                    if call_id is not None:
                                                        seen_tool_call_ids[call_id] = result_dict
                                                    if function_call.name in SIDE_EFFECT_TOOLS:
                                                        seen_signatures[sig] = result_dict

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

                                        # Deliberate: keep this session alive after the tool
                                        # response - the next user turn continues on the SAME
                                        # session. Do not break/restart here.
                                        continue

                                    if self.stop_event.is_set():
                                        break
                                    logger.debug(
                                        "Live interaction complete; waiting for the next turn"
                                    )
                                    if suppressed_signatures:
                                        logger.debug(
                                            "Interaction suppressed %d duplicate-signature "
                                            "tool call(s)",
                                            suppressed_signatures,
                                        )
                            except asyncio.CancelledError:
                                logger.warning("receive_worker cancelled")
                                raise
                            except websockets.exceptions.ConnectionClosed as e:
                                logger.error(f"receive_worker: server disconnected: {e}", exc_info=True)
                                raise
                            except Exception as e:
                                logger.error(f"receive_worker failed: {e}", exc_info=True)
                                raise
                            else:
                                logger.warning("receive_worker stopped")

                        async def synthetic_input_worker():
                            logger.info("Started Synthetic Input Worker...")
                            try:
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
                            except asyncio.CancelledError:
                                logger.warning("synthetic_input_worker cancelled")
                                raise
                            else:
                                logger.warning("synthetic_input_worker exited normally")

                        tasks = [
                            asyncio.create_task(mic_worker()),
                            asyncio.create_task(receive_worker()),
                            asyncio.create_task(synthetic_input_worker())
                        ]

                        # Supervised teardown. Required order:
                        #   worker ends -> surface failure/cancellation ->
                        #   cancel siblings -> clear state + flush stale mic
                        #   input -> leave async-with (SDK closes the session)
                        #   -> backoff -> reconnect with fresh workers.
                        try:
                            done, _pending = await asyncio.wait(
                                tasks, return_when=asyncio.FIRST_COMPLETED
                            )
                        except asyncio.CancelledError:
                            for t in tasks:
                                if not t.done():
                                    t.cancel()
                            await asyncio.gather(*tasks, return_exceptions=True)
                            self._session_active.clear()
                            raise

                        for t in done:
                            if t.cancelled():
                                # Never swallow cancellation: a worker that
                                # ended via CancelledError must propagate so
                                # the supervisor (and run_loop) shuts down
                                # instead of silently reconnecting.
                                logger.warning("Worker task cancelled; leaving session")
                                for s in tasks:
                                    if not s.done():
                                        s.cancel()
                                await asyncio.gather(*tasks, return_exceptions=True)
                                self._session_active.clear()
                                self._clear_input_queue()
                                raise asyncio.CancelledError()
                            exc = t.exception()
                            if isinstance(exc, asyncio.CancelledError):
                                logger.warning("Worker task cancelled; leaving session")
                                for s in tasks:
                                    if not s.done():
                                        s.cancel()
                                await asyncio.gather(*tasks, return_exceptions=True)
                                self._session_active.clear()
                                self._clear_input_queue()
                                raise exc
                            if exc is not None:
                                logger.error(f"Worker task failed: {exc}", exc_info=exc)
                            else:
                                logger.info("Worker task exited normally")

                        logger.info("A worker finished; tearing down session for reconnect")
                        for t in tasks:
                            if not t.done():
                                t.cancel()
                        # Wait for cancellations to propagate (workers log their reason)
                        await asyncio.gather(*tasks, return_exceptions=True)

                        self._session_active.clear()   # mic stops feeding the old session
                        self._clear_input_queue()      # stale queued mic audio is dropped
                        logger.debug(
                            "Session mic totals: received=%d sent=%d dropped_stale=%d",
                            self._mic_stats["received"] - mic_baseline["received"],
                            self._mic_stats["sent"] - mic_baseline["sent"],
                            self._mic_stats["dropped_stale"] - mic_baseline["dropped_stale"],
                        )
                        # NOTE: no manual session.close() here - leaving the
                        # `async with` below lets the SDK close the session.

                    # --- Left `async with`: the SDK has closed the session. ---
                    self._session_active.clear()  # never leave set after disconnect
                    if self.stop_event.is_set():
                        break

                    self._reconnect_attempts += 1
                    max_delay = 60
                    base_delay = min(2 ** self._reconnect_attempts, max_delay)
                    delay = base_delay + random.uniform(0, 1)
                    logger.info(f"Reconnecting in {delay:.1f}s (attempt {self._reconnect_attempts})...")
                    if text_callback:
                        text_callback("system", f"Reconnecting... ({delay:.1f}s)")
                    await asyncio.sleep(delay)

                except asyncio.CancelledError:
                    # Cancellation is not a reconnectable failure: clear
                    # session state and propagate so callers see it.
                    self._session_active.clear()
                    raise
                except Exception as e:
                    logger.error(f"Agent connection dropped (auto-reconnecting): {e}", exc_info=True)
                    play_earcon("fail")
                    self._session_active.clear()
                    self._clear_input_queue()

                    self._reconnect_attempts += 1
                    max_delay = 60
                    base_delay = min(2 ** self._reconnect_attempts, max_delay)
                    delay = base_delay + random.uniform(0, 1)
                    logger.info(f"Reconnecting in {delay:.1f}s (attempt {self._reconnect_attempts})...")
                    if text_callback:
                        text_callback("system", f"Reconnecting... ({e})")
                    await asyncio.sleep(delay)

        finally:
            # Session state must never leak past shutdown/disconnect.
            self._session_active.clear()
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
    agent.loop = loop
    task = loop.create_task(
        agent.run_loop(volume_cb, quit_cb, text_cb, state_cb, bubble_cb, glow_cb)
    )
    agent._run_task = task
    # A stop requested before the task was published (request_stop() saw no
    # task yet) would otherwise be missed: the flag alone cannot stop an
    # uncooperative coroutine. Either request_stop() cancels the visible
    # task, or this check cancels on its behalf - no interleaving escapes.
    if agent.stop_event.is_set():
        task.cancel()
    try:
        loop.run_until_complete(task)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        try:
            # Stop flag first (workers unwind cooperatively), then cancel
            # anything still stuck so close() never sees pending tasks.
            agent.stop_event.set()
            if not task.done():
                task.cancel()
                try:
                    loop.run_until_complete(task)
                except asyncio.CancelledError:
                    pass
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            agent._run_task = None
            loop.close()
