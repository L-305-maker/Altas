FROM python:3.13-alpine
USER 65534:65534
WORKDIR /tmp
CMD ["python", "-I", "-"]
