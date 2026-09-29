from google.genai import types
from PIL import Image

try:
    img = Image.new('RGB', (100, 100))
    # We want to check if the SDK validates this. We can't fully invoke it without an open session.
    # But we can check types.LiveSendRealtimeInputParameters
    obj = types.LiveSendRealtimeInputParameters(video=img)
    print("Serialization OK!")
except Exception as e:
    print(f"FAILED: {e}")
