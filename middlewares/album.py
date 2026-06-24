import asyncio
from typing import Any, Callable, Dict, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import Message

class AlbumMiddleware(BaseMiddleware):
    """
    Middleware to group messages belonging to the same media group (album)
    so they are handled as a single event.
    """
    def __init__(self, latency: float = 0.4):
        self.latency = latency
        self.album_cache = {}
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any]
    ) -> Any:
        if not isinstance(event, Message) or not event.media_group_id:
            return await handler(event, data)

        media_group_id = event.media_group_id

        if media_group_id not in self.album_cache:
            self.album_cache[media_group_id] = [event]
            await asyncio.sleep(self.latency)
            
            messages = self.album_cache.pop(media_group_id, [])
            if not messages:
                return
            
            messages.sort(key=lambda x: x.message_id)
            data["album"] = messages
            return await handler(event, data)
        
        self.album_cache[media_group_id].append(event)
        return
