FROM python:3.8-alpine

LABEL repository="https://github.com/step-security/publish-unit-test-result-action"
LABEL homepage="https://github.com/step-security/publish-unit-test-result-action"

LABEL com.github.actions.name="Publish Test Results"
LABEL com.github.actions.description="A GitHub Action to publish test results."

RUN apk add --no-cache --upgrade expat libuuid

COPY python/requirements.txt /action/
RUN apk add --no-cache build-base libffi-dev; \
    pip install --upgrade --force --no-cache-dir pip && \
    pip install --upgrade --force --no-cache-dir -r /action/requirements.txt; \
    apk del build-base libffi-dev

COPY python/publish /action/publish
COPY python/publish_test_results.py /action/

ENTRYPOINT ["python", "/action/publish_test_results.py"]
