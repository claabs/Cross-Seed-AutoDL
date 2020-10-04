FROM python:3-alpine

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY CrossSeedAutoDL.py .

VOLUME [ "/downloads", "/torrents" ]

ENTRYPOINT [ "python", "CrossSeedAutoDL.py", "--parse-dir", "-i", "/downloads", "--save-path", "/torrents" ]
