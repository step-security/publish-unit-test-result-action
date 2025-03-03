FROM python:3.13-alpine@sha256:323a717dc4a010fee21e3f1aac738ee10bb485de4e7593ce242b36ee48d6b352

LABEL repository="https://github.com/step-security/publish-unit-test-result-action"
LABEL homepage="https://github.com/step-security/publish-unit-test-result-action"

LABEL com.github.actions.name="Publish Test Results"
LABEL com.github.actions.description="A GitHub Action to publish test results."
LABEL com.github.actions.icon="check-circle"
LABEL com.github.actions.color="green"

RUN apk add --no-cache --upgrade expat libuuid

COPY python/requirements-post-3.8.txt /action/requirements.txt
RUN apk add --no-cache build-base libffi-dev; \
    pip install --upgrade --force --no-cache-dir pip && \
    pip install --upgrade --force --no-cache-dir -r /action/requirements.txt; \
    apk del build-base libffi-dev

COPY python/publish /action/publish
COPY python/publish_test_results.py /action/

ENTRYPOINT ["python", "/action/publish_test_results.py"]
