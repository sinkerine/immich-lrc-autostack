FROM python:3.13.9-alpine3.22
RUN apk add --no-cache tzdata
WORKDIR /app
COPY immich_lrc_autostack.py .
USER 1000:1000
ENTRYPOINT ["python", "-u", "/app/immich_lrc_autostack.py"]
