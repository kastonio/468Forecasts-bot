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

RUN apt-get update && apt-get install -y locales \
    && locale-gen ru_RU.UTF-8 \
    && update-locale
ENV LANG ru_RU.UTF-8
ENV LANGUAGE ru_RU:ru
ENV LC_ALL ru_RU.UTF-8
