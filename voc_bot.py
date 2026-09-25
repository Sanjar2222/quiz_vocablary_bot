import html
import asyncio
import json
import logging
import os
import tempfile
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.client.default import DefaultBotProperties

import db
from ai_service import (
    enrich_word_with_ai,
    enrich_words_batch_with_ai,
    extract_words_from_image_path,
    parse_json_input,
    parse_words_input,
)
from tts_service import get_word_audio

load_dotenv()
TOKEN = os.getenv("token") or os.getenv("BOT_TOKEN")

if not TOKEN:
    raise ValueError("Telegram Bot token topilmadi! .env faylida 'token' mavjudligini tekshiring.")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# ----------------- FSM STATES -----------------
class AddWordStates(StatesGroup):
    collecting_words = State()     # Ketma-ket so'zlarni yozib yig'ish holati
    waiting_for_grammar = State()  # Grammatika tanlash (ixtiyoriy)

class BrowseState(StatesGroup):
    current_index = State()
    mode = State()  # 'all' or 'unlearned'

# ----------------- AUDIO TRACKER & AUTO-CLEANER -----------------
temp_voice_messages: dict[int, int] = {}

async def cleanup_temp_voice(chat_id: int):
    """Deletes temporary pronunciation voice message so chat remains 100% clean"""
    msg_id = temp_voice_messages.pop(chat_id, None)
    if msg_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass

# ----------------- KEYBOARDS -----------------
MENU_BUTTONS = [
    "🎯 Quiz (Takrorlash)",
    "➕ So'z qo'shish",
    "📖 Lug'at (Varoqlash ⬅️ ➡️)",
    "🚨 O'rganilmagan so'zlar",
    "📊 Mening statistikam",
    "📥 JSON yuklash"
]

def get_main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎯 Quiz (Takrorlash)"), KeyboardButton(text="➕ So'z qo'shish")],
            [KeyboardButton(text="📖 Lug'at (Varoqlash ⬅️ ➡️)"), KeyboardButton(text="🚨 O'rganilmagan so'zlar")],
            [KeyboardButton(text="📊 Mening statistikam"), KeyboardButton(text="📥 JSON yuklash")]
        ],
        resize_keyboard=True
    )

def get_collecting_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Tayyor (Saqlash)")],
            [KeyboardButton(text="❌ Bekor qilish")]
        ],
        resize_keyboard=True
    )

def get_quiz_keyboard(word_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="I know", callback_data=f"quiz_know_{word_id}"),
                InlineKeyboardButton(text="I do not know", callback_data=f"quiz_dont_{word_id}")
            ],
            [
                InlineKeyboardButton(text="⏹ To'xtatish", callback_data="quiz_stop")
            ]
        ]
    )

def get_quiz_dont_know_keyboard(word_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔊 Talaffuz", callback_data=f"tts_{word_id}"),
                InlineKeyboardButton(text="Keyingi so'z ➡️", callback_data="quiz_next_word")
            ],
            [
                InlineKeyboardButton(text="⏹ To'xtatish", callback_data="quiz_stop")
            ]
        ]
    )

def get_browse_keyboard(index: int, total: int, word_id: int, mode: str = "all"):
    if total <= 1:
        prev_idx = 0
        next_idx = 0
    else:
        prev_idx = (index - 1) % total
        next_idx = (index + 1) % total

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"browse_{mode}_{prev_idx}"),
                InlineKeyboardButton(text=f"{index + 1} / {total}", callback_data="noop"),
                InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"browse_{mode}_{next_idx}")
            ],
            [
                InlineKeyboardButton(text="⏮ 1-so'z", callback_data=f"browse_{mode}_0"),
                InlineKeyboardButton(text="🔊 Talaffuz", callback_data=f"tts_{word_id}"),
                InlineKeyboardButton(text="Oxirgi ⏭", callback_data=f"browse_{mode}_{total - 1}")
            ],
            [
                InlineKeyboardButton(text="🗑 O'chirish", callback_data=f"del_{word_id}_{index}_{mode}"),
                InlineKeyboardButton(text="🔙 Bosh menyu", callback_data="close_browse")
            ]
        ]
    )

# ----------------- START & HELP -----------------
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await db.init_db()
    text = (
        f"👋 <b>Assalomu alaykum, {message.from_user.first_name}!</b>\n\n"
        "Bu sizning <b>Aqlli Lug'at va Quiz Bot</b>ingiz.\n\n"
        "📌 <b>Imkoniyatlar:</b>\n"
        "• <b>So'z qo'shish:</b> Bitta yoki bir nechta so'z (masalan: <i>apple, banana</i>).\n"
        "• <b>Rasm tashlash:</b> Kitob yoki konspekt rasmini tashlang — bot so'zlarni o'zi ajratib oladi!\n"
        "• <b>JSON tashlash:</b> Lug'atingizni to'liq JSON fayl yoki matn ko'rinishida yuboring.\n"
        "• <b>Grammatika tanlash:</b> Misol (example) qaysi grammatikada tuzilishini qo'lda yozasiz.\n"
        "• <b>Quiz (AI-siz, tez va qulay):</b> So'zni taxmin qilasiz. Bilsangiz — 3 kunga suriladi, bilmasangiz — o'rganilmaganlar ro'yxatiga qo'shiladi.\n"
        "• <b>Varoqlash:</b> Lug'atdagi so'zlarni oldin/orqaga o'tkazib ko'rish va talaffuzini eshitish.\n\n"
        "Quyidagi menyudan kerakli bo'limni tanlang:"
    )
    await message.answer(text, reply_markup=get_main_menu())

def get_skip_grammar_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⚡️ Standart qo'shish (Grammatikasiz)", callback_data="skip_grammar")
            ]
        ]
    )

async def execute_enrichment_and_save(event, words: list[str], grammar_choice: str = "-"):
    chat_id = event.chat.id if isinstance(event, Message) else event.message.chat.id
    status_msg = await bot.send_message(
        chat_id=chat_id,
        text=f"⏳ AI <b>{len(words)} ta so'zni</b> tayyorlamoqda... Kuting...",
        reply_markup=get_main_menu()
    )

    enriched_items = await enrich_words_batch_with_ai(words, grammar_note=grammar_choice)

    added_count = 0
    results_preview = []

    for enriched in enriched_items:
        await db.add_or_update_word(
            word=enriched["word"],
            translation=enriched["translation"],
            example_en=enriched["example_en"],
            example_uz=enriched["example_uz"],
            grammar_note=enriched["grammar_note"]
        )
        added_count += 1
        if len(results_preview) < 5:
            w_safe = html.escape(enriched['word'])
            tr_safe = html.escape(enriched['translation'] or '—')
            en_safe = html.escape(enriched['example_en'] or '—')
            uz_safe = html.escape(enriched['example_uz'] or '—')
            results_preview.append(
                f"• <b>{w_safe}</b> — {tr_safe}\n"
                f"  🇬🇧 {en_safe}\n"
                f"  🇺🇿 {uz_safe}"
            )

    all_words = await db.get_all_words()
    total_count = len(all_words)
    preview_text = "\n\n".join(results_preview)
    g_display = html.escape(grammar_choice if grammar_choice != '-' else 'Standart')
    final_text = (
        f"🎉 <b>Muvaffaqiyatli saqlandi!</b>\n"
        f"Qo'shilgan yangi so'zlar: <b>{added_count} ta</b>\n"
        f"Lug'atdagi jami so'zlar: <b>{total_count} ta</b>\n"
        f"Grammatika: <b>{g_display}</b>\n\n"
        f"<b>Namunalar:</b>\n{preview_text}"
    )

    view_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"📖 Qo'shilgan so'zlarni ko'rish ({total_count}/{total_count})",
                    callback_data=f"browse_all_{total_count - 1}"
                )
            ]
        ]
    )

    try:
        await status_msg.edit_text(final_text, reply_markup=view_kb)
    except Exception:
        await bot.send_message(chat_id, final_text, reply_markup=view_kb)

# ----------------- SO'Z QO'SHISH (KETMA-KET YIG'ISH REJIMI) -----------------
@router.message(F.text == "➕ So'z qo'shish")
async def start_add_words(message: Message, state: FSMContext):
    await state.clear()
    await state.update_data(collected_words=[])
    await message.answer(
        "📝 <b>Ketma-ket so'z qo'shish rejimi ishga tushdi!</b>\n\n"
        "Inglizcha so'zlarni bitta-bitta (yoki bir nechta) yozib yuboravering.\n"
        "Har bir yuborgan so'zingiz ro'yxatga qo'shilib boradi.\n\n"
        "Barcha so'zlarni yozib bo'lgach, pastdagi <b>'✅ Tayyor (Saqlash)'</b> tugmasini bosing:",
        reply_markup=get_collecting_keyboard()
    )
    await state.set_state(AddWordStates.collecting_words)

@router.message(AddWordStates.collecting_words, F.text == "❌ Bekor qilish")
async def cancel_collecting(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "❌ So'z qo'shish bekor qilindi. Bosh menyuga qaytildi.",
        reply_markup=get_main_menu()
    )

@router.message(AddWordStates.collecting_words, F.text == "✅ Tayyor (Saqlash)")
async def finish_collecting(message: Message, state: FSMContext):
    data = await state.get_data()
    words = data.get("collected_words", [])
    if not words:
        await message.answer(
            "⚠️ Hali birorta ham so'z kiritmadingiz!\n"
            "Iltimos, so'z yoki iborani yozib yuboring (yoki '❌ Bekor qilish'ni bosing):",
            reply_markup=get_collecting_keyboard()
        )
        return

    words_preview = ", ".join(words[:15]) + ("..." if len(words) > 15 else "")
    await message.answer(
        f"📋 <b>Jami {len(words)} ta so'z yig'ildi:</b>\n"
        f"<i>{words_preview}</i>\n\n"
        "✍️ <i>(Ixtiyoriy)</i> <b>Misol gaplar qaysi grammatikada tuzilsin?</b>\n"
        "Qo'lda yozing (masalan: <code>Past Simple</code>, <code>Conditionals</code>...)\n"
        "yoki shunchaki pastdagi <b>'Standart qo'shish'</b> tugmasini bosing:",
        reply_markup=get_skip_grammar_keyboard()
    )
    await state.set_state(AddWordStates.waiting_for_grammar)

@router.message(AddWordStates.collecting_words, F.text, ~F.text.in_(MENU_BUTTONS))
async def collect_words_input(message: Message, state: FSMContext):
    new_words = parse_words_input(message.text)
    if not new_words:
        await message.answer("⚠️ So'z aniqlanmadi. Iltimos, inglizcha so'z yozing:")
        return

    data = await state.get_data()
    collected = data.get("collected_words", [])

    added_this_step = []
    already_in_list = []
    for w in new_words:
        if w not in collected:
            collected.append(w)
            added_this_step.append(w)
        else:
            already_in_list.append(w)

    await state.update_data(collected_words=collected)
    total_now = len(collected)

    if added_this_step:
        words_str = ", ".join(added_this_step)
        reply = f"➕ <b>{words_str}</b> ro'yxatga qo'shildi! (Jami: <b>{total_now} ta</b>)\n\n"
    else:
        reply = f"ℹ️ Bu so'z(lar) allaqachon ro'yxatda bor edi. (Jami: <b>{total_now} ta</b>)\n\n"

    reply += "Yana so'z yuborishingiz yoki tugatgach pastdagi <b>'✅ Tayyor (Saqlash)'</b> tugmasini bosishingiz mumkin."
    await message.answer(reply, reply_markup=get_collecting_keyboard())

@router.callback_query(F.data == "skip_grammar")
async def skip_grammar_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    words = data.get("collected_words") or data.get("pending_words") or []
    await state.clear()
    if not words:
        await callback.answer("So'zlar topilmadi!")
        return
    try:
        await callback.message.delete()
    except Exception:
        pass
    await execute_enrichment_and_save(callback, words, grammar_choice="-")
    await callback.answer()

@router.message(AddWordStates.waiting_for_grammar, F.text, ~F.text.in_(MENU_BUTTONS))
async def process_grammar_and_enrich(message: Message, state: FSMContext):
    data = await state.get_data()
    words = data.get("collected_words") or data.get("pending_words") or []
    grammar_choice = message.text.strip()
    await state.clear()
    await execute_enrichment_and_save(message, words, grammar_choice=grammar_choice)

# ----------------- RASM (OCR) ORQALI SO'Z QO'SHISH -----------------
@router.message(F.photo)
async def handle_photo(message: Message, state: FSMContext):
    status_msg = await message.answer("🔍 Rasm qabul qilindi. Matnlar va so'zlar o'qilmoqda (OCR)...")
    
    photo = message.photo[-1]
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        await bot.download(photo.file_id, destination=tmp_path)
        words = extract_words_from_image_path(tmp_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    if not words:
        await status_msg.edit_text("❌ Rasmdan inglizcha so'zlar ajratib olinmadi. Iltimos, aniqroq rasm yuboring.")
        return

    curr_state = await state.get_state()
    if curr_state == AddWordStates.collecting_words:
        data = await state.get_data()
        collected = data.get("collected_words", [])
        added = [w for w in words if w not in collected]
        collected.extend(added)
        await state.update_data(collected_words=collected)
        await status_msg.edit_text(
            f"📸 Rasmdan <b>{len(added)} ta yangi so'z</b> ro'yxatga qo'shildi!\n"
            f"<i>{', '.join(added[:10])}{'...' if len(added)>10 else ''}</i>\n\n"
            f"Jami yig'ilgan so'zlar: <b>{len(collected)} ta</b>.\n"
            "Yana so'z/rasm yuborishingiz yoki pastdagi <b>'✅ Tayyor (Saqlash)'</b> tugmasini bosishingiz mumkin."
        )
        return

    await state.update_data(collected_words=words)
    words_display = ", ".join(words[:15]) + ("..." if len(words) > 15 else "")
    await status_msg.edit_text(
        f"📸 <b>Rasmdan {len(words)} ta so'z ajratib olindi:</b>\n"
        f"<i>{words_display}</i>\n\n"
        "✍️ <i>(Ixtiyoriy)</i> <b>Misol gaplar qaysi grammatikada tuzilsin?</b>\n"
        "Qo'lda yozing yoki shunchaki <b>'Standart qo'shish'</b> tugmasini bosing:",
        reply_markup=get_skip_grammar_keyboard()
    )
    await state.set_state(AddWordStates.waiting_for_grammar)

# ----------------- JSON FAYL YOKI MATN YUKLASH -----------------
@router.message(F.text == "📥 JSON yuklash")
async def ask_json(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "📥 <b>JSON formatida so'zlar yuklash:</b>\n\n"
        "Siz JSON faylni (.json) hujjat sifatida yuborishingiz yoki to'g'ridan-to'g'ri JSON matnini xabar qilib tashlashingiz mumkin.\n\n"
        "Format namunasi:\n"
        "<code>[\n"
        "  {\"word\": \"apple\", \"translation\": \"olma\"},\n"
        "  {\"word\": \"banana\", \"translation\": \"banan\"}\n"
        "]</code>\n"
        "yoki <code>{\"my_words\": [...]}</code>"
    )

@router.message(F.document)
async def handle_document(message: Message):
    doc = message.document
    if not (doc.file_name and doc.file_name.endswith(".json")):
        await message.answer("⚠️ Iltimos, faqat <b>.json</b> kengaytmali fayl yuboring.")
        return

    status_msg = await message.answer("⏳ JSON fayl yuklab olinmoqda va bazaga qo'shilmoqda...")
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        await bot.download(doc.file_id, destination=tmp_path)
        with open(tmp_path, "r", encoding="utf-8") as f:
            content = f.read()
        words_list = parse_json_input(content)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    if not words_list:
        await status_msg.edit_text("❌ JSON fayl bo'sh yoki noto'g'ri formatda tuzilgan.")
        return

    count = 0
    for item in words_list:
        await db.add_or_update_word(
            word=item["word"],
            translation=item.get("translation", ""),
            example_en=item.get("example_en", ""),
            example_uz=item.get("example_uz", ""),
            grammar_note=item.get("grammar_note", "")
        )
        count += 1

    await status_msg.edit_text(f"🎉 <b>{count} ta so'z</b> muvaffaqiyatli JSON fayldan import qilindi va saqlandi!")

# Direct JSON text message handler
@router.message(F.text.startswith("{") | F.text.startswith("["))
async def handle_json_text(message: Message):
    words_list = parse_json_input(message.text)
    if not words_list:
        return
    count = 0
    for item in words_list:
        await db.add_or_update_word(
            word=item["word"],
            translation=item.get("translation", ""),
            example_en=item.get("example_en", ""),
            example_uz=item.get("example_uz", ""),
            grammar_note=item.get("grammar_note", "")
        )
        count += 1
    await message.answer(f"🎉 <b>{count} ta so'z</b> JSON matnidan lug'atga qo'shildi!")

# ----------------- QUIZ (WITHOUT AI - FAST & SPACED REPETITION) -----------------
@router.message(F.text == "🎯 Quiz (Takrorlash)")
async def start_quiz(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    quiz_words = await db.get_quiz_words(user_id)

    if not quiz_words:
        await message.answer(
            "🎉 <b>Tabriklaymiz!</b> Hozircha takrorlash uchun so'zlar qolmadi.\n"
            "Barcha o'rganilgan so'zlaringiz 3 kundan keyin qayta takrorlash uchun chiqadi.\n"
            "Yangi so'z qo'shish uchun <b>➕ So'z qo'shish</b> tugmasini bosing.",
            reply_markup=get_main_menu()
        )
        return

    word = quiz_words[0]
    await send_quiz_card(message, word)

def format_quiz_word(word_str: str) -> str:
    word_upper = word_str.strip().upper()
    pad_count = max(4, (26 - len(word_upper)) // 2)
    pad = "\u00A0" * pad_count
    # \u200E belgisi Telegram Desktop qatorlarni o'chirib yubormasligini ta'minlaydi
    return f"\u200E\n\u200E\n{pad}<b>{word_upper}</b>{pad}\n\u200E\n\u200E"

async def send_quiz_card(target, word: dict):
    text = format_quiz_word(word["word"])
    keyboard = get_quiz_keyboard(word["id"])
    if isinstance(target, Message):
        await cleanup_temp_voice(target.chat.id)
        await target.answer(text, reply_markup=keyboard)
    elif isinstance(target, CallbackQuery):
        await cleanup_temp_voice(target.message.chat.id)
        try:
            await target.message.edit_text(text, reply_markup=keyboard)
        except Exception:
            pass

@router.callback_query(F.data.startswith("quiz_know_"))
async def quiz_know(callback: CallbackQuery):
    word_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id
    await cleanup_temp_voice(callback.message.chat.id)

    # Mark as learned: will show after 3 days
    await db.mark_word_learned(user_id, word_id, days=3)
    await callback.answer("✅ 'I know' — 3 kunga surildi!", show_alert=False)

    # Immediately advance to next word
    quiz_words = await db.get_quiz_words(user_id)
    if not quiz_words:
        await callback.message.edit_text(
            "🎉 <b>Ajoyib natija!</b> Hozircha navbatdagi barcha so'zlarni takrorlab bo'ldingiz.\n"
            "O'rganilgan so'zlar 3 kundan keyin yana quizga qaytadi.",
            reply_markup=None
        )
        return

    await send_quiz_card(callback, quiz_words[0])

@router.callback_query(F.data.startswith("quiz_dont_"))
async def quiz_dont(callback: CallbackQuery):
    word_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id
    await cleanup_temp_voice(callback.message.chat.id)

    # Mark as unlearned
    await db.mark_word_unlearned(user_id, word_id)
    words = await db.get_all_words()
    word = next((w for w in words if w["id"] == word_id), None)

    msg_text = "❌ <b>I DO NOT KNOW</b>\n\n"
    if word:
        w_safe = html.escape(word['word'].upper())
        tr_safe = html.escape(word['translation'] or '—')
        en_safe = html.escape(word['example_en'] or '—')
        uz_safe = html.escape(word['example_uz'] or '—')
        msg_text += (
            f"🇬🇧 So'z: <b>{w_safe}</b>\n"
            f"🇺🇿 Tarjimasi: <b>{tr_safe}</b>\n\n"
            f"📝 <b>Misol (EN):</b> {en_safe}\n"
            f"🇺🇿 <b>Misol (UZ):</b> {uz_safe}\n"
        )
        if word.get("grammar_note"):
            g_safe = html.escape(word['grammar_note'])
            msg_text += f"📌 <b>Grammatika:</b> {g_safe}\n"
    msg_text += "\n<i>Yaxshilab eslab qoling va keyingi so'zga o'ting:</i>"
    
    kb = get_quiz_dont_know_keyboard(word_id)
    await callback.message.edit_text(msg_text, reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data == "quiz_next_word")
async def quiz_next_word(callback: CallbackQuery):
    user_id = callback.from_user.id
    await cleanup_temp_voice(callback.message.chat.id)
    quiz_words = await db.get_quiz_words(user_id)
    if not quiz_words:
        await callback.message.edit_text(
            "🎉 <b>Barcha so'zlar takrorlandi!</b> Hozircha navbatda so'z qolmadi.",
            reply_markup=None
        )
        return
    await send_quiz_card(callback, quiz_words[0])
    await callback.answer()

@router.callback_query(F.data == "quiz_stop")
async def quiz_stop(callback: CallbackQuery):
    await cleanup_temp_voice(callback.message.chat.id)
    await callback.message.edit_text("⏹ <b>Quiz yakunlandi.</b> Bosh menyuga qaytildi.", reply_markup=None)
    await callback.answer()

# ----------------- LUG'ATNI VAROQLASH (NAVIGATION ⬅️ ➡️) -----------------
@router.message(F.text == "📖 Lug'at (Varoqlash ⬅️ ➡️)")
async def browse_all_words(message: Message, state: FSMContext):
    await state.clear()
    words = await db.get_all_words()
    if not words:
        await message.answer("📁 Lug'atingiz hozircha bo'sh. Avval yangi so'zlar qo'shing.")
        return
    await send_browse_card(message, words, index=0, mode="all")

@router.message(F.text == "🚨 O'rganilmagan so'zlar")
async def browse_unlearned_words(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    unlearned = await db.get_unlearned_words(user_id)
    if not unlearned:
        await message.answer("🌟 Sizda hozircha o'rganilmagan yoki qiyin so'zlar yo'q! Hammasi a'lo darajada.")
        return
    await send_browse_card(message, unlearned, index=0, mode="unlearned")

async def send_browse_card(target, words: list, index: int, mode: str = "all"):
    total = len(words)
    if index < 0 or index >= total:
        index = 0
    word = words[index]

    mode_title = "📖 <b>BARCHA SO'ZLAR</b>" if mode == "all" else "🚨 <b>O'RGANILMAGAN SO'ZLAR</b>"
    w_safe = html.escape(word['word'].upper())
    tr_safe = html.escape(word['translation'] or '—')
    en_safe = html.escape(word['example_en'] or '—')
    uz_safe = html.escape(word['example_uz'] or '—')
    text = (
        f"{mode_title} ({index + 1}/{total})\n\n"
        f"🇬🇧 So'z: <b>{w_safe}</b>\n"
        f"🇺🇿 Tarjimasi: <b>{tr_safe}</b>\n\n"
        f"📝 <b>Misol (EN):</b> {en_safe}\n"
        f"🇺🇿 <b>Misol (UZ):</b> {uz_safe}\n"
    )
    if word.get("grammar_note"):
        g_safe = html.escape(word['grammar_note'])
        text += f"📌 <b>Grammatika:</b> {g_safe}\n"

    kb = get_browse_keyboard(index, total, word["id"], mode=mode)

    if isinstance(target, Message):
        await cleanup_temp_voice(target.chat.id)
        await target.answer(text, reply_markup=kb)
    elif isinstance(target, CallbackQuery):
        await cleanup_temp_voice(target.message.chat.id)
        try:
            await target.message.edit_text(text, reply_markup=kb)
        except Exception:
            pass

@router.callback_query(F.data.startswith("browse_"))
async def handle_browse_pagination(callback: CallbackQuery):
    parts = callback.data.split("_")
    mode = parts[1]
    index = int(parts[2])

    if mode == "all":
        words = await db.get_all_words()
    else:
        words = await db.get_unlearned_words(callback.from_user.id)

    if not words:
        await cleanup_temp_voice(callback.message.chat.id)
        await callback.message.edit_text("📁 Ro'yxat bo'sh.")
        await callback.answer()
        return

    await send_browse_card(callback, words, index=index, mode=mode)
    await callback.answer()

# ----------------- TALAFFUZ (AUTO-CLEAN SINGLE VOICE) -----------------
@router.callback_query(F.data.startswith("tts_"))
async def handle_tts(callback: CallbackQuery):
    word_id = int(callback.data.split("_")[1])
    words = await db.get_all_words()
    word = next((w for w in words if w["id"] == word_id), None)
    if not word:
        await callback.answer("So'z topilmadi!")
        return

    chat_id = callback.message.chat.id
    # Eski audioni tozalaymiz (chatda 1 tadan ortiq audio aslo yig'ilmaydi)
    await cleanup_temp_voice(chat_id)

    audio_file = get_word_audio(word["word"])
    voice_msg = await callback.message.reply_voice(audio_file, caption=f"🗣 <b>{word['word']}</b>")
    temp_voice_messages[chat_id] = voice_msg.message_id
    await callback.answer()

    # 6 soniya o'tgach yoki boshqa so'zga o'tganda avtomatik o'chadi
    async def auto_delete(target_msg_id: int):
        await asyncio.sleep(6)
        if temp_voice_messages.get(chat_id) == target_msg_id:
            await cleanup_temp_voice(chat_id)

    asyncio.create_task(auto_delete(voice_msg.message_id))

@router.callback_query(F.data == "close_browse")
async def close_browse(callback: CallbackQuery):
    await cleanup_temp_voice(callback.message.chat.id)
    await callback.message.delete()
    await callback.answer()

@router.callback_query(F.data == "noop")
async def noop_click(callback: CallbackQuery):
    await callback.answer()

# ----------------- O'CHIRISH -----------------
@router.callback_query(F.data.startswith("del_"))
async def handle_delete_word(callback: CallbackQuery):
    parts = callback.data.split("_")
    word_id = int(parts[1])
    index = int(parts[2])
    mode = parts[3]

    await db.delete_word_by_id(word_id)
    await callback.answer("🗑 So'z muvaffaqiyatli o'chirildi!", show_alert=True)

    if mode == "all":
        words = await db.get_all_words()
    else:
        words = await db.get_unlearned_words(callback.from_user.id)

    if not words:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer("📁 Barcha so'zlar tugadi.")
        return

    new_index = min(index, len(words) - 1)
    await send_browse_card(callback, words, index=new_index, mode=mode)

# ----------------- STATISTIKA -----------------
@router.message(F.text == "📊 Mening statistikam")
async def show_stats(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    stats = await db.get_user_stats(user_id)
    text = (
        "📊 <b>SIZNING SHAXSIY NATIJALARINGIZ:</b>\n\n"
        f"📚 Jami lug'atdagi so'zlar: <b>{stats['total']} ta</b>\n"
        f"✅ O'rganilgan so'zlar: <b>{stats['learned']} ta</b> (3 kunga surilgan)\n"
        f"🚨 O'rganilmagan/Qiyin so'zlar: <b>{stats['unlearned']} ta</b>\n"
        f"🆕 Hali boshlanmagan so'zlar: <b>{stats['new']} ta</b>\n\n"
        "💡 <i>Muntazam ravishda '🎯 Quiz' tugmasini bosib o'rganishni davom eting!</i>"
    )
    await message.answer(text)

# ----------------- TO'G'RIDAN-TO'G'RI SO'Z YOZISH (FALLBACK) -----------------
@router.message(F.text, ~F.text.in_(MENU_BUTTONS), ~F.text.startswith("/"), ~F.text.startswith("{"), ~F.text.startswith("["))
async def handle_direct_words(message: Message, state: FSMContext):
    curr = await state.get_state()
    if curr:
        return

    words = parse_words_input(message.text)
    if not words:
        return

    await state.update_data(collected_words=words)
    await message.answer(
        f"✅ <b>{len(words)} ta so'z</b> qabul qilindi: <i>{', '.join(words[:5])}{'...' if len(words)>5 else ''}</i>\n\n"
        "✍️ <i>(Ixtiyoriy)</i> <b>Misol gaplar qaysi grammatikada tuzilsin?</b>\n"
        "Qo'lda yozing yoki shunchaki pastdagi <b>'Standart qo'shish'</b> tugmasini bosing:\n\n"
        "💡 <i>Ketma-ket bir nechta so'z yig'ib qo'shish uchun menyudan <b>'➕ So'z qo'shish'</b> tugmasini bosing.</i>",
        reply_markup=get_skip_grammar_keyboard()
    )
    await state.set_state(AddWordStates.waiting_for_grammar)

# ----------------- MAIN RUNNER -----------------
async def main():
    print("🚀 Baza ishga tushirilmoqda...")
    await db.init_db()
    print("🧹 Eski webhook tozalanmoqda...")
    await bot.delete_webhook(drop_pending_updates=True)
    print("🤖 Bot muvaffaqiyatli ishga tushdi...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot to'xtatildi.")