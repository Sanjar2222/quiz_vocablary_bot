import re
import json
import asyncio
from deep_translator import GoogleTranslator
from g4f.client import Client
from rapidocr_onnxruntime import RapidOCR

# Lazy-loaded OCR engine to save memory
_ocr_engine = None

def get_ocr_engine():
    global _ocr_engine
    if _ocr_engine is None:
        _ocr_engine = RapidOCR()
    return _ocr_engine

translator_en_uz = GoogleTranslator(source="en", target="uz")
translator_auto_uz = GoogleTranslator(source="auto", target="uz")
ai_client = Client()

def parse_words_input(text: str) -> list[str]:
    """Parses text containing words separated by commas, newlines, or bullets"""
    raw_items = re.split(r"[,\n;\t•]+", text)
    cleaned = []
    for item in raw_items:
        clean = item.strip().lower()
        clean = re.sub(r"[^\w\s-]", "", clean).strip()
        if clean and len(clean) >= 2:
            cleaned.append(clean)
    # Deduplicate while preserving order
    return list(dict.fromkeys(cleaned))

def parse_json_input(json_str: str) -> list[dict]:
    """Parses JSON text or file content into a list of word dictionaries"""
    try:
        data = json.loads(json_str)
        if isinstance(data, dict):
            words = data.get("my_words", [])
        elif isinstance(data, list):
            words = data
        else:
            return []
        
        parsed = []
        for item in words:
            if isinstance(item, dict) and "word" in item:
                w = item["word"].strip().lower()
                if w:
                    parsed.append({
                        "word": w,
                        "translation": item.get("translation", ""),
                        "example_en": item.get("example_en", ""),
                        "example_uz": item.get("example_uz", ""),
                        "grammar_note": item.get("grammar_note", "")
                    })
            elif isinstance(item, str):
                w = item.strip().lower()
                if w:
                    parsed.append({
                        "word": w,
                        "translation": "",
                        "example_en": "",
                        "example_uz": "",
                        "grammar_note": ""
                    })
        return parsed
    except Exception as e:
        print(f"JSON parsing error: {e}")
        return []

def extract_words_from_image_path(image_path: str) -> list[str]:
    """Uses RapidOCR to read text from an image and extracts meaningful vocabulary words"""
    engine = get_ocr_engine()
    result, _ = engine(image_path)
    if not result:
        return []

    # Combine extracted lines
    full_text = " ".join([line[1] for line in result if line and len(line) > 1])
    if not full_text.strip():
        return []

    # Attempt AI extraction to get clean vocabulary list
    try:
        response = ai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "You are an English language teacher. Extract key English vocabulary words or phrasal verbs from the provided text. Ignore common stop words (a, an, the, is, are, in, of, on, at, by) and numbers. Output ONLY a comma-separated list of lower-case words/phrases, nothing else."
                },
                {"role": "user", "content": f"Text:\n{full_text[:2000]}"}
            ]
        )
        ai_output = response.choices[0].message.content.strip()
        words = parse_words_input(ai_output)
        if words:
            return words
    except Exception as e:
        print(f"AI image vocab extraction fallback due to: {e}")

    # Fallback regex extraction if AI is busy
    raw_tokens = re.findall(r"\b[A-Za-z-]{3,}\b", full_text.lower())
    stop_words = {"the", "and", "that", "have", "for", "not", "with", "you", "this", "but", "his", "from", "they"}
    meaningful = [w for w in raw_tokens if w not in stop_words]
    return list(dict.fromkeys(meaningful))[:30]

async def enrich_word_with_ai(word: str, grammar_note: str = "") -> dict:
    """Translates a single word using enrich_words_batch_with_ai."""
    results = await enrich_words_batch_with_ai([word], grammar_note=grammar_note)
    if results:
        return results[0]
    return {
        "word": word,
        "translation": "",
        "example_en": "",
        "example_uz": "",
        "grammar_note": grammar_note if grammar_note != "-" else ""
    }

async def enrich_words_batch_with_ai(words: list[str], grammar_note: str = "") -> list[dict]:
    """
    Translates and creates example sentences for a list of words in a single AI batch.
    Uses AI JSON generation and falls back gracefully to deep_translator per word if needed.
    """
    clean_words = []
    for w in words:
        cw = w.strip().lower()
        if cw and cw not in clean_words:
            clean_words.append(cw)

    if not clean_words:
        return []

    def _sync_batch_worker():
        grammar_prompt = f" All example sentences MUST strictly follow the grammar rule/tense: '{grammar_note}'." if grammar_note and grammar_note.strip() != "-" else ""
        words_formatted = ", ".join([f'"{w}"' for w in clean_words])
        prompt = (
            f"You are a professional English-Uzbek dictionary and language teacher.\n"
            f"Target English words/phrases: [{words_formatted}]\n"
            f"{grammar_prompt}\n"
            "Return a strictly valid JSON object with a single key \"items\", containing an array of objects for each word with keys:\n"
            "- \"word\": (the exact English word/phrase)\n"
            "- \"translation\": (accurate Uzbek translation of the word)\n"
            "- \"example_en\": (one short, natural English sentence demonstrating the word)\n"
            "- \"example_uz\": (accurate Uzbek translation of the example sentence)\n"
            "Output JSON only."
        )

        results_map = {}
        try:
            response = ai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a professional English-Uzbek lexicographer. Respond ONLY in valid JSON."},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"}
            )
            raw_text = response.choices[0].message.content.strip()
            if raw_text.startswith("```"):
                raw_text = re.sub(r"^```[a-zA-Z]*\n?", "", raw_text)
                raw_text = re.sub(r"\n?```$", "", raw_text)
            data = json.loads(raw_text)
            items = data.get("items", [])
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and "word" in item:
                        w_key = item["word"].strip().lower()
                        results_map[w_key] = {
                            "word": w_key,
                            "translation": item.get("translation", ""),
                            "example_en": item.get("example_en", ""),
                            "example_uz": item.get("example_uz", ""),
                            "grammar_note": grammar_note if grammar_note != "-" else ""
                        }
        except Exception as e:
            print(f"Batch AI JSON enrichment error: {e}")

        # Ensure all requested words are present
        final_list = []
        for w in clean_words:
            if w in results_map:
                final_list.append(results_map[w])
            else:
                tr = ""
                try:
                    tr = translator_en_uz.translate(w)
                except Exception:
                    pass
                final_list.append({
                    "word": w,
                    "translation": tr or "",
                    "example_en": f"Example sentence with {w}.",
                    "example_uz": f"{w} bilan misol gap.",
                    "grammar_note": grammar_note if grammar_note != "-" else ""
                })
        return final_list

    return await asyncio.to_thread(_sync_batch_worker)

