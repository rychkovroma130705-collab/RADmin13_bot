# RADmin13_bot — бот-защитник от спама

Телеграм-бот для верификации новых участников и фильтрации спама.

## Быстрый старт
1. Склонируйте репозиторий.
2. Установите зависимости: `pip install -r requirements.txt`
3. Создайте файл `.env` и заполните:
BOT_TOKEN=
DEFAULT_ACTION_MODE=notify_admin
VERIFICATION_TIMEOUT=180
AUTO_DELETE_UNVERIFIED=True

LLM_API_KEY=
LLM_ACCESS_ID=
LLM_MODEL=OpenAI
LLM_TIMEOUT=8
4. Отредактируйте `stopwords.txt` при необходимости.
5. Запустите: `python main.py`

## Основные команды
- `/help` – список всех команд
- `/setmode delete|notify_admin` – режим реакции на спам
- `/llm on|off|status` – включение/выключение проверки через LLM
- (и другие, см. `/help`)

## Требования
- Python 3.10+
- aiogram 3.x
- SQLite (встроена)

Примечание: для работы LLM необходимо указать `LLM_API_KEY` и `LLM_ACCESS_ID` в `.env`.
