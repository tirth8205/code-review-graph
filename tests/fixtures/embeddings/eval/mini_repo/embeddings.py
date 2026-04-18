"""Mini embedding index for MRR eval fixture."""


def cosine_similarity(a, b):
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def embed_text(text):
    tokens = text.lower().split()
    return [float(len(t)) for t in tokens]


class EmbeddingIndex:
    def __init__(self):
        self.store = {}

    def add(self, qname, vec):
        self.store[qname] = vec

    def search(self, query_vec, limit=10):
        scored = []
        for qn, vec in self.store.items():
            scored.append((qn, cosine_similarity(query_vec, vec)))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]
