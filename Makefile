# Makefile для проекта Clean Telegram Chat

.PHONY: help build run compose-run clean data install local-run rebuild

# Переменные
IMAGE_NAME=clean-telegram-chat
DATA_DIR=data

# По умолчанию показать помощь
help: ## Показать эту справку
	@echo "Доступные команды:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-15s %s\n", $$1, $$2}'

# Создание папки для данных
data: ## Создать папку data для хранения сессий и кэша
	mkdir -p $(DATA_DIR)

# Сборка Docker образа
build: ## Собрать Docker образ
	docker build -t $(IMAGE_NAME) .

# Запуск через Docker
run: data ## Запустить контейнер через docker run
	docker run -it --rm -v $(PWD)/$(DATA_DIR):/app/data $(IMAGE_NAME)

# Запуск через Docker Compose
compose-run: data ## Запустить контейнер через docker-compose
	docker-compose run --rm $(IMAGE_NAME)

# Очистка Docker ресурсов
clean: ## Очистить неиспользуемые Docker ресурсы
	docker system prune -f
	docker image rm $(IMAGE_NAME) 2>/dev/null || true

# Локальная установка зависимостей (без Docker)
install: ## Установить зависимости локально
	pip install -r requirements.txt

# Локальный запуск (без Docker)
local-run: ## Запустить скрипт локально (требует установленного Python и зависимостей)
	python clean_my_messages.py

# Полная пересборка
rebuild: clean build ## Пересобрать образ с нуля