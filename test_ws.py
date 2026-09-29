import asyncio
from google import genai
from google.genai import types

async def main():
    client = genai.Client()
    try:
        async with client.aio.live.connect(model="gemini-2.0-flash-exp", config=types.LiveConnectConfig()) as session:
            print("Connected")
            # Try concurrent sends
            b1 = types.Blob(data=b'1'*2048, mime_type='audio/pcm;rate=16000')
            t1 = asyncio.create_task(session.send_realtime_input(media=b1))
            t2 = asyncio.create_task(session.send_realtime_input(media=b1))
            await asyncio.gather(t1, t2)
            print("Concurrent sends succeeded")
    except Exception as e:
        print(f"Failed: {e}")

asyncio.run(main())
