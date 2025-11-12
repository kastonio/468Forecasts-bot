# Берём полноценный образ Python 3.12
FROM python:3.12

# Задаём рабочую директорию внутри контейнера
WORKDIR /app

# Копируем зависимости и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь проект внутрь контейнера
COPY . .

# Указываем команду для запуска бота
CMD ["python", "main.py"]
