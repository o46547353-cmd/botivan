import logging
import json
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo, InputMediaDocument

async def send_payment_receipt_to_admin(bot: Bot, chat_id: int, photo_file_id: str, caption: str, reply_markup: InlineKeyboardMarkup):
    """
    Sends a payment receipt (which can be a single photo/video/document or an album of multiple media)
    to a specific admin chat.
    """
    is_album = False
    media_items = []
    
    if photo_file_id:
        if photo_file_id.startswith("[") and photo_file_id.endswith("]"):
            try:
                media_items = json.loads(photo_file_id)
                is_album = isinstance(media_items, list) and len(media_items) > 1
            except Exception:
                is_album = False

    if is_album:
        media_group = []
        for idx, item in enumerate(media_items):
            file_id = item.get("file_id")
            m_type = item.get("type", "photo")
            # Only the first media item gets the caption and parse_mode in a media group
            cap = caption if idx == 0 else None
            parse_m = "HTML" if idx == 0 else None
            
            if m_type == "video":
                media_group.append(InputMediaVideo(media=file_id, caption=cap, parse_mode=parse_m))
            elif m_type == "document":
                media_group.append(InputMediaDocument(media=file_id, caption=cap, parse_mode=parse_m))
            else:
                media_group.append(InputMediaPhoto(media=file_id, caption=cap, parse_mode=parse_m))
        
        try:
            # Send the media group first
            await bot.send_media_group(chat_id=chat_id, media=media_group)
            # Then send the action buttons (inline keyboard) as a separate message
            await bot.send_message(
                chat_id=chat_id,
                text=caption,
                reply_markup=reply_markup,
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Failed to send media group to admin {chat_id}: {e}")
    else:
        # Check if the single file is a video or photo/default
        # Wait, if photo_file_id is JSON but is a list of size 1
        if len(media_items) == 1:
            photo_file_id = media_items[0].get("file_id")
            m_type = media_items[0].get("type", "photo")
        else:
            # Try to infer type, or default to photo
            m_type = "photo"

        if photo_file_id:
            try:
                if m_type == "video":
                    await bot.send_video(
                        chat_id=chat_id,
                        video=photo_file_id,
                        caption=caption,
                        reply_markup=reply_markup,
                        parse_mode="HTML"
                    )
                elif m_type == "document":
                    await bot.send_document(
                        chat_id=chat_id,
                        document=photo_file_id,
                        caption=caption,
                        reply_markup=reply_markup,
                        parse_mode="HTML"
                    )
                else:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=photo_file_id,
                        caption=caption,
                        reply_markup=reply_markup,
                        parse_mode="HTML"
                    )
            except Exception as e:
                logging.error(f"Failed to send single media to admin {chat_id}: {e}")
        else:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=caption,
                    reply_markup=reply_markup,
                    parse_mode="HTML"
                )
            except Exception as e:
                logging.error(f"Failed to send fallback message to admin {chat_id}: {e}")
