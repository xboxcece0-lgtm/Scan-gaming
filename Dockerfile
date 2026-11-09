FROM python:3.12-alpine
RUN apk add --no-cache iproute2 iputils tzdata
WORKDIR /app
COPY server ./server
COPY web ./web
ENV DB_PATH=/data/scan-gaming.db PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
CMD ["python3", "server/app.py"]
