import io
import json

from okf_aws.embeddings import (
    build_hierarchy_filter,
    create_index_if_absent,
    delete_vector,
    embed_text,
    put_vector,
    query_vectors,
)


class FakeBody:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b


class FakeBedrock:
    def __init__(self):
        self.calls = []

    def invoke_model(self, **kwargs):
        self.calls.append(kwargs)
        body = json.loads(kwargs["body"])
        assert body["dimensions"] == 512
        assert body["normalize"] is True
        return {"body": FakeBody({"embedding": [0.1] * 512})}


class NotFound(Exception):
    def __init__(self):
        self.response = {"Error": {"Code": "NotFoundException"}}


class FakeS3Vectors:
    def __init__(self, index_exists=False):
        self.index_exists = index_exists
        self.created = None
        self.put = []
        self.deleted = []
        self.queries = []

    def get_index(self, **kwargs):
        if not self.index_exists:
            raise NotFound()
        return {"index": {}}

    def create_index(self, **kwargs):
        self.created = kwargs
        self.index_exists = True

    def put_vectors(self, **kwargs):
        self.put.append(kwargs)

    def delete_vectors(self, **kwargs):
        self.deleted.append(kwargs)

    def query_vectors(self, **kwargs):
        self.queries.append(kwargs)
        return {
            "vectors": [
                {
                    "key": "sales/orders/tables/races",
                    "distance": 0.1,
                    "metadata": {"title": "Races"},
                }
            ]
        }


def test_embed_text_returns_vector():
    br = FakeBedrock()
    v = embed_text(br, "hello world")
    assert len(v) == 512
    assert br.calls[0]["modelId"] == "amazon.titan-embed-text-v2:0"


def test_create_index_if_absent_creates_with_frozen_params():
    s3v = FakeS3Vectors(index_exists=False)
    created = create_index_if_absent(s3v, vector_bucket="vb", index_name="idx")
    assert created is True
    assert s3v.created["dimension"] == 512
    assert s3v.created["distanceMetric"] == "cosine"
    assert s3v.created["dataType"] == "float32"
    assert s3v.created["metadataConfiguration"]["nonFilterableMetadataKeys"] == [
        "title",
        "description",
        "s3_key",
    ]


def test_create_index_if_absent_noop_when_present():
    s3v = FakeS3Vectors(index_exists=True)
    assert create_index_if_absent(s3v, vector_bucket="vb", index_name="idx") is False
    assert s3v.created is None


def test_put_vector_uses_tagged_union_data_key():
    s3v = FakeS3Vectors()
    put_vector(
        s3v,
        vector_bucket="vb",
        index_name="idx",
        key="sales/orders/tables/races",
        embedding=[0.0] * 512,
        metadata={"data_domain": "sales"},
    )
    item = s3v.put[0]["vectors"][0]
    assert item["key"] == "sales/orders/tables/races"
    assert "float32" in item["data"]  # tagged union, NOT a flat "vector" key
    assert "vector" not in item["data"]
    assert len(item["data"]["float32"]) == 512


def test_delete_vector():
    s3v = FakeS3Vectors()
    delete_vector(s3v, vector_bucket="vb", index_name="idx", key="k")
    assert s3v.deleted[0]["keys"] == ["k"]


def test_query_vectors_requests_metadata_and_distance():
    s3v = FakeS3Vectors()
    out = query_vectors(
        s3v,
        vector_bucket="vb",
        index_name="idx",
        query_embedding=[0.0] * 512,
        top_k=5,
        metadata_filter={"data_domain": {"$eq": "sales"}},
    )
    q = s3v.queries[0]
    assert q["returnMetadata"] is True
    assert q["returnDistance"] is True
    assert q["filter"] == {"data_domain": {"$eq": "sales"}}
    assert out[0]["key"] == "sales/orders/tables/races"


def test_build_hierarchy_filter():
    assert build_hierarchy_filter() is None
    assert build_hierarchy_filter(data_domain="sales") == {
        "data_domain": {"$eq": "sales"}
    }
    f = build_hierarchy_filter(data_domain="sales", dataset="orders", tags=["a", "b"])
    assert f["$and"][0] == {"data_domain": {"$eq": "sales"}}
    assert {"tags": {"$in": ["a", "b"]}} in f["$and"]
