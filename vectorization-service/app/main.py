"""FastAPI entrypoint for the vectorization-service.

Wires the object graph in a lifespan context: Postgres pool -> vector store, embedder, ingest
pipeline, and the Kafka consumer that drives everything. The consumer runs for the app's lifetime
and is stopped cleanly on shutdown.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.config import get_settings
from app.db.pool import create_pool
from app.db.reranker import build_reranker
from app.db.vector_store import VectorStore
from app.ingest.contextualizer import build_context_generator
from app.ingest.embedder import build_embedder
from app.ingest.pipeline import IngestPipeline
from app.kafka.consumer import IngestionConsumer
from app.logging_config import configure_logging
from app.observability import ObservabilityMiddleware, metrics_response

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    logger.info("starting %s", settings.service_name)

    pool = await create_pool(settings)
    embedder = build_embedder(settings)
    context_generator = build_context_generator(settings)
    store = VectorStore(pool)
    pipeline = IngestPipeline(settings, embedder, store, context_generator)
    consumer = IngestionConsumer(settings, pipeline)
    reranker = build_reranker(settings)

    app.state.settings = settings
    app.state.pool = pool
    app.state.embedder = embedder
    app.state.store = store
    app.state.consumer = consumer
    app.state.reranker = reranker

    await consumer.start()
    try:
        yield
    finally:
        await consumer.stop()
        await embedder.aclose()
        await context_generator.aclose()
        await pool.close()
        logger.info("stopped %s", settings.service_name)


app = FastAPI(title="Vectorization Service", version="0.1.0", lifespan=lifespan)
app.add_middleware(ObservabilityMiddleware)
# Demo-only convenience, not the real access-control boundary — see ai-service/app/main.py's
# identical comment for the full reasoning (no auth exists in front of /search yet either).
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)
app.include_router(router)


@app.get("/metrics")
async def metrics():
    return metrics_response()
