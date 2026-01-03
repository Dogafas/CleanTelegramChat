FROM python:3.11-slim

# Установка системных зависимостей, если нужны (для Pyrogram может потребоваться)
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Установка рабочей директории
WORKDIR /app

# Копирование requirements.txt и установка зависимостей
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование всех файлов проекта
COPY . .

# Команда запуска
CMD ["python", "clean_my_messages.py"]