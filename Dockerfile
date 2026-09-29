FROM python:3.12-slim

WORKDIR /opt/spa-control
COPY app ./app
COPY static ./static

ENV SPA_BIND=0.0.0.0 \
    SPA_HTTP_PORT=8080 \
    SPA_PORT=4257 \
    SPA_CONFIG=/var/lib/spa-control/config.json

VOLUME ["/var/lib/spa-control"]
EXPOSE 8080
CMD ["python", "-m", "app.main"]
