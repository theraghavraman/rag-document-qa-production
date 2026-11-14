"""Locust load test; use an isolated test tenant, not production data."""
import os
from locust import HttpUser, between, task


class RagUser(HttpUser):
    wait_time = between(1, 4)

    def on_start(self):
        self.headers = {'X-API-Key': os.environ['RAG_API_KEY']}

    @task(3)
    def list_documents(self):
        self.client.get('/v1/documents?limit=20', headers=self.headers)

    @task(1)
    def query(self):
        self.client.post('/v1/qa/query', headers=self.headers, json={
            'question': 'What does the policy say about returns?', 'top_k': 3,
        }, timeout=120)
