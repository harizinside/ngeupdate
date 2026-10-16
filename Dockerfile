# docker CLI + compose/buildx plugins; python only lives inside the image.
FROM docker:27-cli
RUN apk add --no-cache python3 git openssh-client
COPY server.py /app/server.py
ENV HOST=0.0.0.0 PYTHONUNBUFFERED=1
CMD ["python3", "/app/server.py"]
