from pyrogram import Client

api_id = 14955062  # Замените на ваш API ID
api_hash = "7e313794c7e74534d09b9e88bed00138"  # Замените на ваш API Hash
chat_id = -1002413764329  # ID или username группы


app = Client("my_account", api_id=api_id, api_hash=api_hash)

with app:
    members = app.get_chat_members(chat_id)
    for member in members:
        print(
            f"User ID: {member.user.id}, Username: {member.user.username}, Full Name: {member.user.first_name} {member.user.last_name}"
        )
