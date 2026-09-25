import os
from gtts import gTTS
from aiogram.types import FSInputFile

CACHE_DIR = "audio_cache"
os.makedirs(CACHE_DIR, exist_ok=True)

def get_word_audio(word: str) -> FSInputFile:
    """Returns an FSInputFile of the word's pronunciation, cached on disk to avoid re-generating"""
    clean_word = word.strip().lower()
    file_path = os.path.join(CACHE_DIR, f"{clean_word}.mp3")
    if not os.path.exists(file_path):
        try:
            tts = gTTS(text=clean_word, lang="en", slow=False)
            tts.save(file_path)
        except Exception as e:
            print(f"Error generating audio for {clean_word}: {e}")
    return FSInputFile(file_path, filename=f"{clean_word}.mp3")
