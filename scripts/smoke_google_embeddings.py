"""Construct the Google provider after installing an optional dependency set."""

from importlib import metadata

from code_review_graph.embeddings import GoogleEmbeddingProvider


def main() -> None:
    requirements = metadata.requires("code-review-graph") or []
    assert any(
        requirement.startswith("google-genai")
        and "google-embeddings" in requirement
        for requirement in requirements
    )

    provider = GoogleEmbeddingProvider(api_key="optional-extra-smoke-test")
    try:
        assert provider.name == "google:gemini-embedding-001"
    finally:
        provider._client.close()


if __name__ == "__main__":
    main()
