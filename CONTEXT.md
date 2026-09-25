# Clean Telegram Chat

Инструмент автоматизированной очистки собственных сообщений пользователя в групповых чатах Telegram.

## Language

**Target Chat**:
Чат, в котором аккаунт состоит и который выбран для удаления его сообщений: публичная супергруппа, закрытая супергруппа или обычная группа.
_Avoid_: dialog, channel, left chat, личка

**Public Supergroup**:
Супергруппа с username. Аккаунт в ней состоит.
_Avoid_: channel, public chat

**Private Supergroup**:
Супергруппа без username. Аккаунт в ней состоит.
_Avoid_: closed chat, secret chat, left chat

**Basic Group**:
Обычная группа, не супергруппа. Аккаунт в ней состоит.
_Avoid_: supergroup, closed chat

**Left Chat**:
Чат, в котором аккаунта уже нет. Не Target Chat: свои сообщения там удалить нельзя.
_Avoid_: archived dialog, private supergroup

**Supergroup**:
Тип группового чата в Telegram с расширенными возможностями администрирования и истории сообщений.
_Avoid_: group, channel

**Message Purge**:
Процесс пакетного поиска и безвозвратного удаления сообщений текущего пользователя в выбранном Target Chat.
_Avoid_: clean, wipe, clear

**Message Batch**:
Фиксированная порция найденных сообщений пользователя (обычно до 100 штук), удаляемая за один сетевой запрос с соблюдением интервалов троттлинга.
_Avoid_: chunk, pack, slice

**Telegram Session**:
Персистентное состояние авторизации клиента (файлы сессии и API-ключи), обеспечивающее доступ к Telegram без повторного прохождения двухфакторной аутентификации.
_Avoid_: auth cache, token, credentials

**Session Directory**:
Выделенная папка `sessions/` в корне проекта для хранения персистентных файлов сессий Telegram (`*.session`).
_Avoid_: session folder, accounts dir

**Session Name**:
Идентификатор сессии (имя файла без расширения `.session`), задаваемый пользователем.
_Avoid_: account id, login

**All Sessions**:
Режим последовательной пакетной очистки сообщений во всех сессиях из `Session Directory`.
_Avoid_: batch all, multi session
