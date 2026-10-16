from telethon import TelegramClient, events

API_ID = 12345678
API_HASH = "YOUR_API_HASH"

BOT_USERNAME = "steal_credsbot"

client = TelegramClient("telegram_reader_session", API_ID, API_HASH)


async def main():
    bot = await client.get_entity(BOT_USERNAME)

    print("Existing messages from bot:")
    async for message in client.iter_messages(bot, limit=100):
        if message.text:
            print(f"[{message.date}] {message.text}")

    print("\nWaiting for new messages...")


@client.on(events.NewMessage(chats=BOT_USERNAME))
async def new_message(event):
    print(f"[{event.message.date}] {event.raw_text}")


with client:
    client.loop.run_until_complete(main())
    client.run_until_disconnected()