from functools import lru_cache
from urllib.parse import urlparse
from uuid import UUID

from opensearchpy import OpenSearch
from qdrant_client import QdrantClient, models
from sentence_transformers import SentenceTransformer
from sqlalchemy import select

from .config import get_settings
from .db import Chunk, Document, SessionLocal


def reciprocal_rank_fusion(rankings: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item_id in enumerate(ranking, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda item: item[1], reverse=True)


@lru_cache
def embedding_model() -> SentenceTransformer:
    settings = get_settings()
    return SentenceTransformer(settings.embedding_model, device=settings.embedding_device)


def embed(texts: list[str]) -> list[list[float]]:
    vectors = embedding_model().encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return [vector.tolist() for vector in vectors]


def qdrant() -> QdrantClient:
    return QdrantClient(url=get_settings().qdrant_url, timeout=60)


def opensearch() -> OpenSearch:
    parsed = urlparse(get_settings().opensearch_url)
    return OpenSearch(
        hosts=[{"host": parsed.hostname or "127.0.0.1", "port": parsed.port or 9200}],
        use_ssl=parsed.scheme == "https",
        verify_certs=False,
        timeout=60,
    )


def ensure_indexes(vector_size: int) -> None:
    settings = get_settings()
    qc = qdrant()
    if not qc.collection_exists(settings.qdrant_collection):
        qc.create_collection(
            collection_name=settings.qdrant_collection,
            vectors_config=models.VectorParams(size=vector_size, distance=models.Distance.COSINE),
        )
    oc = opensearch()
    if not oc.indices.exists(settings.opensearch_index):
        oc.indices.create(
            settings.opensearch_index,
            body={
                "settings": {"index": {"number_of_shards": 1, "number_of_replicas": 0}},
                "mappings": {
                    "properties": {
                        "chunk_id": {"type": "keyword"},
                        "document_id": {"type": "keyword"},
                        "text": {"type": "text"},
                    }
                },
            },
        )


def index_chunks(chunks: list[Chunk]) -> None:
    if not chunks:
        raise ValueError("Документ не содержит извлекаемого текста")
    vectors = embed([chunk.child_text for chunk in chunks])
    ensure_indexes(len(vectors[0]))
    settings = get_settings()
    qdrant().upsert(
        collection_name=settings.qdrant_collection,
        wait=True,
        points=[
            models.PointStruct(
                id=str(chunk.id),
                vector=vector,
                payload={"chunk_id": str(chunk.id), "document_id": str(chunk.document_id)},
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        ],
    )
    oc = opensearch()
    for chunk in chunks:
        oc.index(
            index=settings.opensearch_index,
            id=str(chunk.id),
            body={
                "chunk_id": str(chunk.id),
                "document_id": str(chunk.document_id),
                "text": chunk.child_text,
            },
            refresh=False,
        )
    oc.indices.refresh(settings.opensearch_index)


def retrieve(question: str, limit: int = 8) -> tuple[list[dict], int]:
    settings = get_settings()
    vector = embed([question])[0]
    dense = qdrant().query_points(
        collection_name=settings.qdrant_collection, query=vector, limit=50
    ).points
    dense_ids = [str(point.id) for point in dense]
    sparse_result = opensearch().search(
        index=settings.opensearch_index,
        body={"size": 50, "query": {"match": {"text": {"query": question}}}},
    )
    sparse_ids = [hit["_id"] for hit in sparse_result["hits"]["hits"]]
    fused = reciprocal_rank_fusion([dense_ids, sparse_ids])
    candidate_ids = [UUID(item_id) for item_id, _ in fused[:24]]
    if not candidate_ids:
        return [], 0
    with SessionLocal() as session:
        rows = session.execute(
            select(Chunk, Document)
            .join(Document, Chunk.document_id == Document.id)
            .where(Chunk.id.in_(candidate_ids), Document.active.is_(True), Document.status == "ready")
        ).all()
    by_id = {str(chunk.id): (chunk, document) for chunk, document in rows}
    selected: list[dict] = []
    seen_parents: set[tuple[str, int]] = set()
    for item_id, score in fused:
        if item_id not in by_id:
            continue
        chunk, document = by_id[item_id]
        parent_key = (str(document.id), chunk.ordinal)
        if parent_key in seen_parents:
            continue
        seen_parents.add(parent_key)
        selected.append(
            {
                "chunk_id": str(chunk.id),
                "document_id": str(document.id),
                "title": document.title,
                "filename": document.filename,
                "page": chunk.page,
                "parent_text": chunk.parent_text,
                "child_text": chunk.child_text,
                "child_start": chunk.child_start,
                "child_end": chunk.child_end,
                "score": score,
            }
        )
        if len(selected) >= limit:
            break
    return selected, len(fused)
