import re

with open("src/cat_talker/agent.py", "r") as f:
    content = f.read()

# Add queue to __init__
init_target = """        self._is_processing = False"""
init_replacement = """        self._is_processing = False
        self.synthetic_input_queue = asyncio.Queue()
        self.loop = None"""
content = content.replace(init_target, init_replacement)

# Set loop in run_loop
run_loop_target = """    async def run_loop(self,"""
run_loop_replacement = """    async def run_loop(self, volume_callback=None, app_quit_callback=None, text_callback=None,
                       state_callback=None, bubble_callback=None, glow_callback=None):
        self.loop = asyncio.get_running_loop()"""

# Replace entire def run_loop(...): up to self.audio = AudioInterface() to be safe.
reg_run_loop = r"    async def run_loop\(self,.*?\):\n        self\.audio = AudioInterface\(\)"
content = re.sub(reg_run_loop, """    async def run_loop(self, volume_callback=None, app_quit_callback=None, text_callback=None,
                       state_callback=None, bubble_callback=None, glow_callback=None):
        self.loop = asyncio.get_running_loop()
        self.audio = AudioInterface()""", content, flags=re.DOTALL | re.MULTILINE)

# Add synthetic_worker
tasks_target = """                        tasks = [
                            asyncio.create_task(mic_worker()),
                            asyncio.create_task(receive_worker())
                        ]"""

synthetic_worker = """
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
                        ]"""

content = content.replace(tasks_target, synthetic_worker)

# Modify start_agent_in_thread signature and usage
thread_func_target = """def start_agent_in_thread(volume_cb, quit_cb=None, text_cb=None, state_cb=None, bubble_cb=None, glow_cb=None):
    agent = GeminiDesktopAgent()"""
thread_func_replacement = """def start_agent_in_thread(volume_cb, quit_cb=None, text_cb=None, state_cb=None, bubble_cb=None, glow_cb=None, global_agent_ref=None):
    agent = GeminiDesktopAgent()
    if global_agent_ref is not None:
        global_agent_ref.append(agent)"""
content = content.replace(thread_func_target, thread_func_replacement)

with open("src/cat_talker/agent.py", "w") as f:
    f.write(content)

print("Agent patched for Phase 6")
