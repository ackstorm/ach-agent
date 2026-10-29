FROM python:3.13-alpine
RUN adduser -D -u 10001 stub
COPY stub.py /app/stub.py
USER 10001
WORKDIR /app
EXPOSE 8090 8091 8092
CMD ["python", "-u", "/app/stub.py"]
