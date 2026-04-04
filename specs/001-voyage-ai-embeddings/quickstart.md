# Quickstart: Using Voyage AI Embeddings

## 1. Install Dependencies

```bash
pip install code-review-graph[voyage-embeddings]
# or with uv:
uv pip install code-review-graph[voyage-embeddings]
```

## 2. Set Your API Key

```bash
export VOYAGEAI_API_KEY="your-voyage-ai-api-key"
```

## 3. Build the Graph and Embed

```bash
# Build the code graph first
uv run code-review-graph build

# Embed nodes using Voyage AI (via MCP tool or programmatically)
```

When using the MCP server, the `embed_graph_tool` will automatically use Voyage AI if `VOYAGEAI_API_KEY` is set and the provider is selected.

## 4. Programmatic Usage

```python
from code_review_graph.embeddings import get_provider

# Get Voyage AI provider
provider = get_provider("voyage")

# Embed texts
vectors = provider.embed(["def hello(): pass", "class MyClass:"])

# Query
query_vec = provider.embed_query("function that greets")
```

## 5. Custom Model

By default, `voyage-code-3` is used (optimized for code). To use a different model:

```python
provider = get_provider("voyage", model="voyage-3")
```

## 6. Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `VOYAGEAI_API_KEY` | Yes | Your Voyage AI API key |
