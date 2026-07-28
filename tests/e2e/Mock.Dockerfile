FROM python:3.13-slim-bookworm

COPY Efferva/tests/e2e/mock_responses.py /app/mock_responses.py

EXPOSE 18090
CMD ["python", "/app/mock_responses.py"]
