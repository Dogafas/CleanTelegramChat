# Clean Telegram Chat

Программа предназначена для автоматического удаления всех пользовательских сообщений из группового Telegram-чата. Она упрощает процесс очистки вашей истории сообщений в групповых чатах с большой активностью участников и большим количеством сообщений. Ручное удаление может быть трудоемким и требует значительного времени, особенно если необходимо проматывать историю сообщений для поиска собственных записей. Программа решает эту проблему, предоставляя возможность массово удалить ваши сообщения из выбранного вами группового чата.

---

## Вам обязательно понадобится

1. Войти на сайт [my.telegram.org](https://my.telegram.org/).
2. Выбрать ссылку **API development tools**.
3. Создать автономное приложение.
4. Скопировать **app_id** и **app_hash**.
5. (При первом запуске скрипт запросит у вас API ID и API HASH, и сохранит их в файл `cache.json`.)

---

## Установка и запуск

1. Скачайте проект и установите все необходимые зависимости с помощью команды:
   ```bash
   pip install -r requirements.txt

1. Запустите скрипт:
   python clean_my_messages.py
- Альтернативный вариант: запустите сначала файл install.bat, а затем start.bat.

## Аутентификация
При запуске программа потребует от вас ввести в строку **Enter phone number or bot token:** свой номер телефона в международном формате, например: +79874561122.
Подтвердить правильность ввода номер, введя **y** и нажав Enter.
Ввести **код для входа в Telegram**, который придет вам на телефон.
(Если у вас установлена двухфакторная авторизация, ввести пароль).

### Пример использования
1. Просмотрите список супергрупп и выберите те, из которых хотите удалить сообщения, указав их номера через запятую.
2. Скрипт начнет удаление сообщений из выбранных супергрупп. Дождитесь завершения программы.

_Если у вас возникнут вопросы или проблемы, пожалуйста, создайте issue в репозитории проекта_.